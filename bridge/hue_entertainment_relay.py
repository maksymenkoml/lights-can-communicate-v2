"""Host-only, owner-fenced DTLS relay to the selected native Entertainment area."""

import copy
import json
import math
import os
import shutil
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from entertainment_stream import POSITIONS, encode_frame, native_colors
from native_location_cache import NativeLocationCache

import lights_can_communicate_v2 as lan
from lights_can_communicate_v2_core.native_profile import NativeHueProfile


class DtlsConnection:
    def __init__(self, config):
        binary = next(
            (
                str(p)
                for p in (
                    Path("/opt/homebrew/opt/openssl/bin/openssl"),
                    Path("/usr/local/opt/openssl/bin/openssl"),
                )
                if p.is_file()
            ),
            shutil.which("openssl"),
        )
        if not binary:
            raise ValueError("OpenSSL is unavailable")
        self.ready = threading.Event()
        self.process = subprocess.Popen(
            [
                binary,
                "s_client",
                "-dtls1_2",
                "-brief",
                "-quiet",
                "-cipher",
                "PSK-AES128-GCM-SHA256",
                "-psk",
                config["client_key"],
                "-psk_identity",
                config.get("application_id", config["hue_key"]),
                "-connect",
                config["bridge"] + ":2100",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

        def drain():
            try:
                for line in self.process.stderr:
                    if b"CONNECTION ESTABLISHED" in line:
                        self.ready.set()
            except (OSError, ValueError):
                pass

        threading.Thread(target=drain, daemon=True, name="hue-dtls-status").start()
        if not self.ready.wait(3) or self.process.poll() is not None:
            self.close()
            raise OSError("Native DTLS handshake failed")
        os.set_blocking(self.process.stdin.fileno(), False)

    def send(self, frame):
        if self.process.poll() is not None:
            raise OSError("Native DTLS process stopped")
        if os.write(self.process.stdin.fileno(), frame) != len(frame):
            raise OSError("Incomplete native frame")

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=1)
        for stream in (self.process.stdin, self.process.stderr):
            if stream:
                stream.close()


class HueEntertainmentRelay:
    def __init__(
        self, config, requester=None, connection_factory=DtlsConnection, layout_store=None,
        preview=None, profile=None, location_cache_file=None,
    ):
        if not isinstance(profile, NativeHueProfile):
            raise ValueError("An explicit native Hue profile is required")
        self.profile = profile
        self.area_id = profile.area_id
        self.layout_store = layout_store
        self.preview = preview
        if (
            not isinstance(config.get("client_key"), str)
            or len(config["client_key"]) != 32
            or any(x not in "0123456789abcdefABCDEF" for x in config["client_key"])
        ):
            raise ValueError("Invalid selected Entertainment credential")
        self.config = {**config, "bridge": lan.local_ip(config["bridge"]), "area_id": self.area_id}
        self.requester = requester or self.request
        self.connection_factory = connection_factory
        self.connection = None
        self.owned = False
        self.sequence = 0
        self.ownership_checked = float("-inf")
        self.composition = False
        self.applied_roles = None
        self.channel_map = {}
        self.location_cache = (
            NativeLocationCache(location_cache_file, self.area_id,
                                {service for service, _ in self.inventory()[0]})
            if location_cache_file is not None else None
        )
        self.locations = self.location_cache.snapshot() if self.location_cache is not None else {}

    def request(self, method, body=None):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=ctx)
        )
        req = urllib.request.Request(
            "https://"
            + self.config["bridge"]
            + "/clip/v2/resource/entertainment_configuration/"
            + self.area_id,
            method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={
                "hue-application-key": self.config["hue_key"],
                "Content-Type": "application/json",
            },
        )
        try:
            with opener.open(req, timeout=1.5) as response:
                result = json.load(response)
        except urllib.error.HTTPError as error:
            error.close()
            raise
        if not isinstance(result, dict) or result.get("errors"):
            raise ValueError("Native Entertainment request rejected")
        if method == "GET":
            data = result.get("data")
            if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
                raise ValueError("Invalid native Entertainment response")
            return data[0]
        return result

    def inventory(self):
        members = self.profile.expected_members
        roles = ("hue_center", "hue_left", "hue_right", "strip")
        lights = self.profile.light_ids
        return members, roles, lights

    def composition_area(self, area, expected_roles=None, *, persist_locations=True):
        """Validate enrollment; cleanup can validate without depending on cache storage."""
        members, roles, lights = self.inventory()
        canonical = {member: index for index, member in enumerate(members)}
        services = {members[i][0]: role for i, role in enumerate(roles)}
        if area.get("id") != self.area_id:
            raise ValueError("Native area membership changed")
        rows = area.get("light_services")
        if not isinstance(rows, list) or any(
            not isinstance(row, dict) or row.get("rtype") != "light"
            or row.get("rid") not in lights.values() for row in rows
        ):
            raise ValueError("Invalid native area membership")
        selected = {role for role, rid in lights.items() if any(r.get("rid") == rid for r in rows)}
        if len(rows) != len(selected) or (expected_roles is not None
                                        and selected != set(expected_roles)):
            raise ValueError("Native area membership changed")
        channels = area.get("channels")
        if not isinstance(channels, list):
            raise ValueError("Invalid native channel layout")
        mapping = {}
        seen = set()
        for channel in channels:
            if (not isinstance(channel, dict) or type(channel.get("channel_id")) is not int
                    or not 0 <= channel["channel_id"] <= 255
                    or channel["channel_id"] in mapping
                    or not isinstance(channel.get("members"), list)
                    or len(channel["members"]) != 1):
                raise ValueError("Invalid native channel layout")
            member = channel["members"][0]
            if (not isinstance(member, dict) or type(member.get("index")) is not int
                    or not isinstance(member.get("service"), dict)
                    or member["service"].get("rtype") != "entertainment"
                    or not isinstance(member["service"].get("rid"), str)):
                raise ValueError("Invalid native channel member")
            key = (member["service"].get("rid"), member["index"])
            if key not in canonical or key in seen:
                raise ValueError("Native channel layout changed")
            seen.add(key)
            mapping[channel["channel_id"]] = canonical[key]
        expected = {member for member in members if services[member[0]] in selected}
        if seen != expected:
            raise ValueError("Native channel selection changed")
        locations = area.get("locations")
        rows = locations.get("service_locations") if isinstance(locations, dict) else None
        if not isinstance(rows, list):
            raise ValueError("Invalid native service locations")
        observed = {}
        for row in rows:
            service = row.get("service") if isinstance(row, dict) else None
            if (not isinstance(service, dict) or service.get("rtype") != "entertainment"
                    or not isinstance(service.get("rid"), str)
                    or service["rid"] not in services or service["rid"] in observed):
                raise ValueError("Invalid native location service")
            positions = row.get("positions")
            if (not isinstance(positions, list) or not 1 <= len(positions) <= 12
                    or any(not isinstance(position, dict) or set(position) != {"x", "y", "z"}
                           or any(type(value) not in (int, float) or not math.isfinite(value)
                                  or not -1 <= value <= 1 for value in position.values())
                           for position in positions)):
                raise ValueError("Invalid native location positions")
            observed[service["rid"]] = {
                "service": {"rid": service["rid"], "rtype": "entertainment"},
                "positions": copy.deepcopy(positions),
            }
            if "equalization_factor" in row:
                factor = row["equalization_factor"]
                if (type(factor) not in (int, float) or not math.isfinite(factor)
                        or not 0 <= factor <= 1):
                    raise ValueError("Invalid native location equalization")
                observed[service["rid"]]["equalization_factor"] = factor
            elif "equalization_factor" in self.locations.get(service["rid"], {}):
                observed[service["rid"]]["equalization_factor"] = (
                    self.locations[service["rid"]]["equalization_factor"]
                )
        if set(observed) != {service for service, role in services.items() if role in selected}:
            raise ValueError("Native location selection changed")
        if persist_locations:
            if self.location_cache is not None:
                self.location_cache.merge(observed)
                self.locations = self.location_cache.snapshot()
            else:
                self.locations.update(observed)
        return selected, mapping

    def area(self, expected_roles=None, *, persist_locations=True):
        area = self.requester("GET")
        if not isinstance(area, dict) or area.get("status") not in ("active", "inactive"):
            raise ValueError("Unconfirmed native Entertainment state")
        owner = area.get("active_streamer")
        if area["status"] == "active" and (
            not isinstance(owner, dict)
            or not isinstance(owner.get("rid"), str)
            or not owner["rid"]
        ):
            raise ValueError("Unconfirmed native stream owner")
        if self.composition:
            self.composition_area(area, expected_roles, persist_locations=persist_locations)
            return area
        services = area.get("light_services")
        if (
            not isinstance(services, list)
            or len(services) != 4
            or any(not isinstance(item, dict) or not isinstance(item.get("rid"), str)
                   for item in services)
            or any(item.get("rtype") != "light" for item in services)
        ):
            raise ValueError("Invalid native area membership")
        resources = self.profile.resource_ids
        if area.get("id") != self.area_id or {
            x.get("rid") for x in services
        } != resources:
            raise ValueError("Native area membership changed")
        expected = self.profile.expected_members
        channels = area.get("channels")
        if not isinstance(channels, list) or any(
            not isinstance(channel, dict)
            or type(channel.get("channel_id")) is not int
            or not isinstance(channel.get("members"), list)
            or len(channel["members"]) != 1
            or not isinstance(channel["members"][0], dict)
            or not isinstance(channel["members"][0].get("service"), dict)
            or type(channel["members"][0].get("index")) is not int
            or channel["members"][0]["service"].get("rtype") != "entertainment"
            for channel in channels
        ):
            raise ValueError("Invalid native channel layout")
        channels = sorted(channels, key=lambda x: x["channel_id"])
        if len(channels) != 12 or any(
            channel["channel_id"] != index
            or len(channel["members"]) != 1
            or (channel["members"][0]["service"].get("rid"), channel["members"][0].get("index"))
            != expected[index]
            for index, channel in enumerate(channels)
        ):
            raise ValueError("Native channel layout changed")
        return area

    def ours(self, area):
        owner = area.get("active_streamer")
        return isinstance(owner, dict) and owner.get("rid") == self.config.get(
            "application_id", self.config["hue_key"]
        )

    def start(self, composition_profile=None):
        members, roles, _ = self.inventory()
        if composition_profile is not None:
            self.composition = True
            selected = composition_profile.get("native_roles")
            if (not isinstance(selected, (tuple, list, set, frozenset))
                    or any(role not in roles for role in selected)
                    or len(set(selected)) != len(selected)):
                raise ValueError("Invalid native selection")
            selected = frozenset(selected)
        elif self.composition:
            selected = frozenset(self.inventory()[1])
        else:
            selected = None
        area = self.area(self.applied_roles if self.owned else None)
        if area.get("status") == "active":
            if not self.ours(area):
                if self.composition and not selected:
                    if self.connection is not None:
                        self.connection.close()
                        self.connection = None
                    self.applied_roles = frozenset()
                    self.channel_map = {}
                    self.owned = False
                    return True
                return False
            self.owned = True
            self.stop()
            area = self.area()
        elif self.connection is not None:
            self.connection.close()
            self.connection = None
        # stop() confirms release, then start reads again. A different streamer may
        # acquire the area in between; every fresh read must fence both writes below.
        if area["status"] != "inactive":
            self.owned = False
            if self.composition and not selected:
                self.applied_roles = frozenset()
                self.channel_map = {}
                return True
            return False
        if self.composition:
            self.owned = False  # The observed area is now inactive; no streaming lease remains.
            observed, mapping = self.composition_area(area)
            if not selected:
                self.applied_roles = frozenset()
                self.channel_map = {}
                self.owned = False
                return True
            if observed != selected:
                rows = []
                for i, role in enumerate(roles):
                    if role not in selected:
                        continue
                    service = members[i][0]
                    if self.location_cache is not None and service not in self.locations:
                        raise ValueError("Native location metadata is unavailable")
                    # After restart an excluded service may lack location metadata. Use its
                    # known canonical position; physical sampling is projected upstream.
                    source = (12, 11, 13, 6)[i]
                    row = self.locations.get(service, {
                        "service": {"rid": service, "rtype": "entertainment"},
                        "positions": [dict(zip(("x", "y", "z"), POSITIONS[source]))],
                    })
                    rows.append(copy.deepcopy(row))
                self.requester("PUT", {"locations": {"service_locations": rows}})
                area = self.area(selected)
                _, mapping = self.composition_area(area, selected)
                if area["status"] == "active":
                    # A foreign owner appearing during readback must never be started over.
                    return False
            self.applied_roles, self.channel_map = selected, mapping
        self.owned = True
        self.ownership_checked = float("-inf")
        try:
            self.requester("PUT", {"action": "start"})
            confirmation = self.area(self.applied_roles)
            if confirmation["status"] != "active" or not self.ours(confirmation):
                raise ValueError("Native stream ownership not confirmed")
            self.connection = self.connection_factory(self.config)
            return True
        except Exception:
            self.stop()
            raise

    def recover(self):
        loader = getattr(self.layout_store, "sync_profile", None)
        if loader is not None and loader() is not None:
            self.composition = True
        self.owned = True
        self.stop()

    def send(self, frame):
        if self.composition and self.applied_roles == frozenset() and not self.owned:
            return
        if not self.owned or self.connection is None:
            raise ValueError("No owned native stream")
        if time.monotonic() - self.ownership_checked >= 1:
            area = self.area(self.applied_roles)
            if area.get("status") != "active" or not self.ours(area):
                raise ValueError("Native stream ownership changed")
            if self.composition:
                _, self.channel_map = self.composition_area(area, self.applied_roles)
            self.ownership_checked = time.monotonic()
        scales = (
            {
                role: self.layout_store.scale(role, fallback)
                for role, fallback in {
                    "strip": 1,
                    "hue_left": 1,
                    "hue_center": 0.5,
                    "hue_right": 1,
                }.items()
            }
            if self.layout_store is not None
            else None
        )
        channels = native_colors(frame, scales)
        if self.composition:
            # IDs are bridge-owned; source/calibration indices remain canonical.
            mapping = self.channel_map
            canonical = {index: channels[index] for index in mapping.values()}
            output = {returned: channels[index] for returned, index in mapping.items()}
        else:
            canonical = output = channels
        self.connection.send(encode_frame(self.area_id, output, self.sequence))
        self.sequence = (self.sequence + 1) % 256
        if self.preview is not None:
            try:
                self.preview.record_native_channels(canonical)
            except Exception:
                pass  # Passive telemetry cannot interrupt the native stream.

    def stop(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None
        if self.owned:
            area = self.area(self.applied_roles, persist_locations=False)
            if area.get("status") == "active" and self.ours(area):
                self.requester("PUT", {"action": "stop"})
                confirmation = self.area(self.applied_roles, persist_locations=False)
                if confirmation["status"] == "active" and self.ours(confirmation):
                    raise OSError("Native stream release not confirmed")
            self.owned = False
