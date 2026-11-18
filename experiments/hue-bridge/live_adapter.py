"""Host-side pilot: authenticated Hue transition targets -> two pinned Govee."""

import argparse
import colorsys
import copy
import hmac
import json
import math
import os
import secrets
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

if Path(__file__).is_file():
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from desk_layout import LayoutStore  # noqa: E402
from entertainment_stream import GRADIENT_MODE  # noqa: E402
from govee_segments import interpolate, segment_data  # noqa: E402
from homekit_power import (  # noqa: E402
    HomeKitPower,
    HomeKitRequests,
    SignatureVerifier,
    response_signature,
)
from hue_entertainment_relay import HueEntertainmentRelay  # noqa: E402
from hue_strip_proxy import HueStripProxy, PinnedHueClient, xy_color  # noqa: E402
from pilot_transport import PilotTransport  # noqa: E402
from power_owner import PowerOwner  # noqa: E402
from power_requests import PowerRequests, PowerRequestUnavailable, validate_capability  # noqa: E402
from stream_diagnostics import StreamDiagnostics  # noqa: E402
from sync_health import export as export_diagnostics  # noqa: E402
from video_output import VideoOutput  # noqa: E402

import lights_can_communicate_v2 as lan  # noqa: E402
from lights_can_communicate_v2_core.discovery import DeviceCatalog  # noqa: E402
from lights_can_communicate_v2_core.hue_discovery import HueDiscoveryReader  # noqa: E402
from lights_can_communicate_v2_core.live_preview import OutputPreviewStore, build_preview  # noqa: E402
from lights_can_communicate_v2_core.native_profile import NativeHueProfile  # noqa: E402


def number(value, low, high):
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise ValueError("Expected a finite number")
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError("Numeric value is outside the accepted range")
    return value


def normalize_state(state, scale, segment_count=0):
    if not isinstance(state, dict) or not isinstance(state.get("on"), bool):
        raise ValueError("Expected a light state with boolean on")
    if not state["on"]:
        return {"on": False}
    brightness = number(state.get("bri", 254), 0, 254)
    result = {
        "on": True,
        "brightness": max(1, round(brightness / 254 * 100 * lan.parse_brightness_scale(scale))),
    }
    mode = state.get("colormode", "xy")
    if "segments" in state or state.get("_color_kind") == "gradient":
        if segment_count != 7:
            raise ValueError("Zoned output requires an explicitly enabled H6076 profile")
        if "segments" in state:
            stops = state["segments"]
        else:
            gradient = state.get("gradient")
            if not isinstance(gradient, dict):
                raise ValueError("Expected a gradient object")
            points = gradient.get("points")
            if not isinstance(points, list) or not 1 <= len(points) <= 5:
                raise ValueError("Expected a bounded gradient palette")
            stops = []
            for point in points:
                if not isinstance(point, dict) or not isinstance(point.get("color"), dict):
                    raise ValueError("Expected a gradient color point")
                color = xy_color(point["color"].get("xy"))
                stops.append(lan.xy_to_rgb(color["x"], color["y"]))
        result["segments"] = interpolate(stops)
    elif mode == "rgb":
        rgb = state.get("rgb")
        if not isinstance(rgb, list) or len(rgb) != 3:
            raise ValueError("Expected RGB triple")
        result["rgb"] = tuple(round(number(value, 0, 255)) for value in rgb)
    elif mode == "ct":
        mirek = number(state.get("ct"), 100, 1000)
        result["kelvin"] = max(2000, min(9000, round(1_000_000 / mirek)))
    elif mode == "hs":
        hue = number(state.get("hue"), 0, 65535)
        saturation = number(state.get("sat"), 0, 254)
        result["rgb"] = tuple(
            round(value * 255) for value in colorsys.hsv_to_rgb(hue / 65535, saturation / 254, 1)
        )
    else:
        xy = state.get("xy")
        if not isinstance(xy, list) or len(xy) != 2:
            raise ValueError("Expected xy color")
        result["rgb"] = lan.xy_to_rgb(number(xy[0], 0, 1), number(xy[1], 0.0001, 1))
    return result


def as_rgb(target):
    if "segments" in target:
        return target["segments"][3]
    if "rgb" in target:
        return target["rgb"]
    # Approximate CT only while crossing color modes. Final CT uses the white channel.
    # Curve coefficients: https://tannerhelland.com/2012/09/18/convert-temperature-rgb-algorithm-code.html
    temperature = target["kelvin"] / 100
    red = 255 if temperature <= 66 else 329.698727446 * (temperature - 60) ** -0.1332047592
    green = (
        99.4708025861 * math.log(temperature) - 161.1195681661
        if temperature <= 66
        else 288.1221695283 * (temperature - 60) ** -0.0755148492
    )
    blue = (
        255 if temperature >= 66 else 138.5177312231 * math.log(temperature - 10) - 305.0447927307
    )
    return tuple(max(0, min(255, round(value))) for value in (red, green, blue))


def blend(first, last, fraction):
    if fraction >= 1:
        return dict(last)
    fraction = max(0, fraction)
    result = {
        "on": True,
        "brightness": round(
            first["brightness"] + (last["brightness"] - first["brightness"]) * fraction
        ),
    }
    if "segments" in first or "segments" in last:
        start = first.get("segments") or [as_rgb(first)] * 7
        end = last.get("segments") or [as_rgb(last)] * 7
        result["segments"] = [
            tuple(round(a + (b - a) * fraction) for a, b in zip(left, right))
            for left, right in zip(start, end)
        ]
    elif "kelvin" in first and "kelvin" in last:
        result["kelvin"] = round(first["kelvin"] + (last["kelvin"] - first["kelvin"]) * fraction)
    else:
        result["rgb"] = tuple(
            round(start + (end - start) * fraction)
            for start, end in zip(as_rgb(first), as_rgb(last))
        )
    return result


def native_target(status, fallback):
    if not status or "brightness" not in status:
        return dict(fallback)
    result = {"on": True, "brightness": max(1, min(100, status["brightness"]))}
    if status.get("colorTemInKelvin", 0):
        result["kelvin"] = max(2000, min(9000, status["colorTemInKelvin"]))
    else:
        result["rgb"] = tuple(status.get("color", {}).get(key, 0) for key in ("r", "g", "b"))
    return result


class PilotController:
    def __init__(
        self,
        targets,
        sender=lan.send_to_target,
        clock=time.monotonic,
        wall_clock=time.time,
        layout_store=None,
        power_owner=None,
        configuration="sync",
    ):
        if configuration not in ("sync", "scene"):
            raise ValueError("Unknown output configuration")
        # "sync" is the Sync composition; "scene" is the output without Sync, which
        # uses scene membership, the scene correction and the scene fade.
        self.configuration = configuration
        self.layout_store = layout_store
        self.power_owner = power_owner
        self.observer_epoch = secrets.token_hex(8)
        self.power_revisions = {}
        self.targets = copy.deepcopy(targets)
        if len(self.targets) != 2 or {item.get("side") for item in self.targets} != {
            "left",
            "right",
        }:
            raise ValueError("The pilot requires exactly the selected left and right targets")
        self.by_side = {item["side"]: item for item in self.targets}
        self.packet_rates = {
            item["side"]: number(item["max_commands_per_second"], 1, 100)
            if "max_commands_per_second" in item
            else None
            for item in self.targets
        }
        self.gradient_packet_rates = {
            item["side"]: number(item["max_gradient_packets_per_second"], 1, 100)
            if "max_gradient_packets_per_second" in item
            else None
            for item in self.targets
        }
        self.next_send = {}
        self.ids = {lan.device_key(item) for item in self.targets}
        if len(self.ids) != 2:
            raise ValueError("Pinned IDs must be distinct")
        self.sender, self.clock, self.wall_clock = sender, clock, wall_clock
        self.lock = threading.RLock()
        self.statuses, self.jobs, self.accepted, self.last_sent, self.last_refresh = (
            {},
            {},
            {},
            {},
            {},
        )
        self.commands_sent = 0
        self.pause_gate = None
        self.skipped_state = {side: 0 for side in self.by_side}
        self.rejected_sends = {side: 0 for side in self.by_side}
        self.color_samples = {side: deque(maxlen=500) for side in self.by_side}

    def update_statuses(self, statuses):
        with self.lock:
            for key, value in statuses.items():
                if key in self.ids and value[0] is not None:
                    self.statuses[key] = value

    def power_observations(self):
        with self.lock:
            now = self.clock()
            result = {}
            for side, device in self.by_side.items():
                native, seen = self.statuses.get(lan.device_key(device), (None, 0))
                sample = native.get("native_sample") if isinstance(native, dict) else None
                valid = (isinstance(native, dict) and native.get("identity_verified") is True
                         and type(native.get("onOff")) is int and native["onOff"] in {0, 1}
                         and type(seen) in (int, float) and math.isfinite(seen)
                         and 0 <= now - seen <= 0.75
                         and type(sample) in (int, float) and math.isfinite(sample)
                         and 0 <= now - sample <= 0.75)
                result[side] = {"on": bool(native["onOff"]) if valid else None,
                                "sample": sample if valid else None}
            return result

    def included(self, side):
        name = "present" if self.configuration == "scene" else "enabled"
        member = getattr(self.layout_store, name, None)
        return member(side) if member is not None else True

    def brightness_scale_for(self, side, base):
        """Return this configuration's brightness correction for a side."""
        name = "scene_scale" if self.configuration == "scene" else "scale"
        return getattr(self.layout_store, name, self.layout_store.scale)(side, base)

    def scene_gains(self, side):
        """Return seven bottom-to-top scene fade gains, or None when nothing is faded."""
        loader = getattr(self.layout_store, "scene_fade", None)
        if self.configuration != "scene" or loader is None:
            return None
        fade = loader()
        gains = fade.get(side) if fade else None
        return gains if gains is not None and min(gains) < 1 else None

    def fade_target(self, side, target):
        """Attenuate a scene target by the scene fade without mutating the job."""
        gains = self.scene_gains(side)
        if gains is None:
            return target
        if "segments" in target or self.by_side[side].get("segment_count", 0) == 7:
            segments = target.get("segments") or [as_rgb(target)] * 7
            target = {key: value for key, value in target.items()
                      if key not in ("rgb", "kelvin")}
            target["segments"] = [
                tuple(round(value * gain) for value in rgb)
                for rgb, gain in zip(segments, gains)
            ]
        else:
            # Without segments the strip's mean gain dims the whole lamp.
            mean = sum(gains) / len(gains)
            target["brightness"] = max(1, min(100, round(target["brightness"] * mean)))
        return target

    def clear_side(self, side):
        """Forget output work while retaining the accepted source ordering fence."""
        with self.lock:
            for values in (self.jobs, self.last_sent, self.last_refresh, self.next_send):
                values.pop(side, None)

    def target(self, side):
        with self.lock:
            if not self.included(side):
                self.clear_side(side)
                return None
            job = self.jobs.get(side)
            if job is None:
                return None
            fraction = (self.clock() - job["start"]) / job["duration"] if job["duration"] else 1
            return blend(job["from"], job["to"], fraction)

    def effective_target(self, side):
        target = self.target(side)
        if target is not None and self.layout_store is not None:
            base = lan.parse_brightness_scale(self.by_side[side].get("brightness_scale", 1))
            scale = self.brightness_scale_for(side, base)
            brightness = (
                target["brightness"] * scale / base
                if base
                else self.jobs[side].get("source_bri", 254) / 254 * 100 * scale
            )
            target["brightness"] = max(1, min(100, round(brightness)))
            target = self.fade_target(side, target)
        return target

    def submit(self, payload):
        if not isinstance(payload, dict) or set(payload) != {
            "side",
            "state",
            "transitiontime",
            "session",
            "sequence",
            "issued_at",
        }:
            raise ValueError("Invalid command shape")
        side = payload["side"]
        if side not in self.by_side:
            raise ValueError("Unknown side")
        session = payload["session"]
        sequence = payload["sequence"]
        if not isinstance(session, str) or not 1 <= len(session) <= 80:
            raise ValueError("Invalid command session")
        if type(sequence) is not int or not 0 <= sequence < 2**63:
            raise ValueError("Invalid command sequence")
        issued = number(payload["issued_at"], self.wall_clock() - 5, self.wall_clock() + 5)
        duration = number(payload["transitiontime"], 0, 1200) / 10
        device = self.by_side[side]
        desired = normalize_state(
            payload["state"], device.get("brightness_scale", 1), device.get("segment_count", 0)
        )
        with self.lock:
            previous = self.accepted.get(side)
            if previous and (
                (session == previous[0] and sequence <= previous[1])
                or (session != previous[0] and issued <= previous[2])
            ):
                return False
            self.accepted[side] = (session, sequence, issued)
            if not desired["on"] or not self.included(side):
                self.clear_side(side)
                return True
            initial = self.target(side)
            if initial is None:
                status = self.statuses.get(lan.device_key(device), (None, 0))[0]
                initial = native_target(status, desired)
                if self.layout_store is not None and status and "brightness" in status:
                    base = lan.parse_brightness_scale(device.get("brightness_scale", 1))
                    scale = self.brightness_scale_for(side, base)
                    gains = self.scene_gains(side)
                    if gains is not None and self.by_side[side].get("segment_count", 0) != 7:
                        # The native brightness already carries the mean fade gain.
                        scale *= sum(gains) / len(gains)
                    if base and scale:
                        initial["brightness"] *= base / scale
            self.jobs[side] = {
                "from": initial,
                "to": desired,
                "start": self.clock(),
                "duration": duration,
                "source_bri": payload["state"].get("bri", 254),
            }
        return True

    def render(self):
        with self.lock:
            for side in self.by_side:
                if not self.included(side):
                    self.clear_side(side)
            if self.pause_gate is not None and not self.pause_gate():
                self.last_sent.clear()
                return
            now = self.clock()
            for side, device in self.by_side.items():
                if not self.included(side):
                    self.clear_side(side)
                    continue
                if self.power_owner is not None:
                    revision = self.power_owner.revision(side)
                    if self.power_revisions.get(side) != revision:
                        self.last_sent.pop(side, None)
                        self.power_revisions[side] = revision
                    if not self.power_owner.allowed(side):
                        self.last_sent.pop(side, None)
                        if side in self.jobs:
                            self.skipped_state[side] += 1
                        continue
                status, seen = self.statuses.get(lan.device_key(device), (None, float("-inf")))
                if not status or status.get("onOff") != 1 or not 0 <= now - seen <= 0.75:
                    self.last_sent.pop(side, None)
                    if side in self.jobs:
                        self.skipped_state[side] += 1
                    continue
                target = self.effective_target(side)
                if target is None:
                    continue
                old = self.last_sent.get(side, {})
                refresh = now - self.last_refresh.get(side, float("-inf")) >= 30
                updates = []
                commands = (
                    [
                        ("ptReal", segment_data(target["segments"])),
                        ("brightness", {"value": target["brightness"]}),
                    ]
                    if "segments" in target
                    else lan.govee_commands(target)
                )
                for name, data in commands:
                    changed = (
                        old.get("brightness") != target["brightness"]
                        if name == "brightness"
                        else (old.get("rgb"), old.get("kelvin"), old.get("segments"))
                        != (target.get("rgb"), target.get("kelvin"), target.get("segments"))
                    )
                    if changed or refresh:
                        updates.append((name, data))
                rate = self.packet_rates[side]
                gradient_rate = self.gradient_packet_rates[side]
                if "segments" in target and gradient_rate is not None:
                    rate = min(rate, gradient_rate) if rate is not None else gradient_rate
                if (
                    updates
                    and rate is not None
                    and now + 1e-9 < self.next_send.get(side, float("-inf"))
                ):
                    continue
                complete = True
                for name, data in updates:
                    try:
                        success = self.sender(device, name, data)
                    except OSError:
                        success = False
                    complete = success and complete
                    self.commands_sent += int(success)
                    if not success:
                        self.rejected_sends[side] += 1
                    elif name == "colorwc":
                        self.color_samples[side].append(
                            {
                                "time": now,
                                "color": data["color"],
                                "kelvin": data["colorTemInKelvin"],
                            }
                        )
                if updates and rate is not None:
                    self.next_send[side] = now + len(updates) / rate
                if complete:
                    self.last_sent[side] = target
                    if refresh:
                        self.last_refresh[side] = now

    def snapshot(self):
        with self.lock:
            return {
                "active_sides": sorted(self.jobs),
                "commands_sent": self.commands_sent,
                "skipped_state": dict(self.skipped_state),
                "rejected_sends": dict(self.rejected_sends),
                "color_samples": {
                    side: list(samples) for side, samples in self.color_samples.items()
                },
                "targets": [
                    {
                        "side": item["side"],
                        "device": lan.device_key(item),
                        "brightness_scale": item.get("brightness_scale", 1),
                        "segment_count": item.get("segment_count", 0),
                        "max_commands_per_second": self.packet_rates[item["side"]],
                        "max_gradient_packets_per_second": self.gradient_packet_rates[item["side"]],
                        "target": self.effective_target(item["side"]),
                        "native": self.statuses.get(lan.device_key(item), (None, 0))[0],
                        "status_age": (
                            self.clock() - self.statuses[lan.device_key(item)][1]
                            if lan.device_key(item) in self.statuses
                            else None
                        ),
                    }
                    for item in self.targets
                ],
            }


def handler_for(controller, token, hue_proxy=None, hue_play_proxies=None, video_output=None,
                power_owner=None, power_token=None, service_info=None, catalog=None, preview=None,
                power_requests=None, homekit=None):
    if power_owner is not None and (
        not isinstance(power_token, str) or not power_token
        or hmac.compare_digest(power_token.encode(), token.encode())
    ):
        raise ValueError("A distinct power owner credential is required")
    broker = power_requests or (PowerRequests(power_owner) if power_owner is not None else None)
    signatures = SignatureVerifier(power_token) if homekit is not None else None
    hue_proxies = dict(hue_play_proxies or {})
    if hue_proxy is not None:
        hue_proxies["strip"] = hue_proxy

    class Handler(BaseHTTPRequestHandler):
        power_requests = broker
        def log_message(self, *args):
            pass

        def respond(self, status, value):
            data = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            nonce = getattr(self, "signed_nonce", None)
            if nonce is not None:
                self.send_header("X-LCC-Response",
                                 response_signature(power_token, nonce, status, data))
            keep_alive = getattr(self, "stream_keep_alive", False)
            self.send_header("Connection", "keep-alive" if keep_alive else "close")
            self.close_connection = not keep_alive
            if keep_alive:
                self.connection.settimeout(2)
            self.end_headers()
            self.wfile.write(data)

        def authorized(self):
            return hmac.compare_digest(self.headers.get("X-Adapter-Token", "").encode(),
                                       token.encode())

        def owner_authorized(self):
            return power_token is not None and hmac.compare_digest(
                self.headers.get("X-Power-Owner-Token", "").encode(), power_token.encode())

        def do_GET(self):
            self.stream_keep_alive = False
            self.signed_nonce = None
            if self.path == "/homekit" and homekit is not None:
                if not signatures.verify("GET", self.path, self.headers, b""):
                    return self.respond(403, {"error": "unauthorized"})
                self.signed_nonce = self.headers["X-LCC-Nonce"]
            elif not self.authorized():
                return self.respond(403, {"error": "unauthorized"})
            if self.path == "/homekit":
                if homekit is None:
                    return self.respond(404, {"error": "not found"})
                return self.respond(200, homekit.state())
            if self.path == "/discovery" and catalog is not None:
                return self.respond(200, catalog.snapshot())
            if self.path not in ("/status", "/diagnostics"):
                return self.respond(404, {"error": "not found"})
            result = controller.snapshot()
            loader = getattr(getattr(controller, "layout_store", None), "sync_profile", None)
            profile = loader() if loader is not None else None
            if profile is not None:
                result["composition"] = {
                    "enabled_roles": profile["enabled_roles"], "signature": profile["signature"],
                }
            if hue_proxy is not None:
                result["hue_strip"] = hue_proxy.snapshot()
            if hue_play_proxies:
                result["hue_play"] = {
                    role: proxy.snapshot() for role, proxy in hue_play_proxies.items()
                }
            if video_output is not None:
                result["video"] = video_output.snapshot()
            if power_owner is not None:
                result["power_owner"] = power_owner.snapshot()
            if service_info is not None:
                result["service"] = dict(service_info)
            if preview is not None:
                try:
                    output_snapshot = preview.snapshot()
                except Exception:
                    output_snapshot = {}
                result["preview"] = build_preview(result, output_snapshot)
            if self.path == "/diagnostics":
                # Allowlisted export: no credentials, addresses or device identifiers.
                return self.respond(200, export_diagnostics(result))
            self.respond(200, result)

        def do_POST(self):
            self.stream_keep_alive = False
            self.signed_nonce = None
            if self.path in ("/power-owner", "/homekit"):
                # The local HomeKit authority fences the legacy remote owner.
                if (power_owner is None or power_token is None
                        or (self.path == "/power-owner") is not (homekit is None)):
                    return self.respond(404, {"error": "not found"})
                # /homekit is signed; its signature covers the body read below.
                if self.path == "/power-owner" and not self.owner_authorized():
                    return self.respond(403, {"error": "unauthorized"})
            elif not self.authorized():
                return self.respond(403, {"error": "unauthorized"})
            if self.path not in ("/command", "/stream", "/power-owner", "/power-request",
                                 "/native-power",
                                 "/homekit") or (
                self.path == "/stream" and video_output is None
            ):
                return self.respond(404, {"error": "not found"})
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 8192:
                    raise ValueError("Invalid request size")
                raw = self.rfile.read(size)
                if self.path == "/homekit":
                    if not signatures.verify("POST", self.path, self.headers, raw):
                        return self.respond(403, {"error": "unauthorized"})
                    self.signed_nonce = self.headers["X-LCC-Nonce"]
                payload = json.loads(raw)
                if self.path == "/power-owner":
                    if not isinstance(payload, dict):
                        raise ValueError("Invalid power owner envelope")
                    capability = validate_capability(payload.get("power_requests"),
                                                     payload.get("states"))
                    envelope = {key: value for key, value in payload.items()
                                if key != "power_requests"}
                    accepted = power_owner.submit(envelope)
                    observed = controller.power_observations()
                    return self.respond(200, {"accepted": accepted,
                                              "power_request_version": 1,
                                              "observer_epoch": controller.observer_epoch,
                                              "observed": observed,
                                              **broker.exchange(envelope, capability,
                                                                observed, accepted)})
                elif self.path == "/homekit":
                    confirmed = homekit.set(payload)
                    return self.respond(200 if confirmed else 503,
                                        {"accepted": confirmed, **homekit.state()})
                elif self.path == "/power-request":
                    if broker is None:
                        return self.respond(503, {"accepted": False})
                    return self.respond(200, {"accepted": broker.request(payload)})
                elif self.path == "/native-power":
                    roles = payload.get("roles") if isinstance(payload, dict) else None
                    if (not isinstance(roles, dict) or not roles
                            or set(roles) - set(hue_proxies)
                            or any(type(value) is not bool for value in roles.values())):
                        raise ValueError("Invalid native power request")
                    try:
                        confirmed = all([hue_proxies[role].set_power(value)
                                         for role, value in roles.items()])
                    except (OSError, ValueError, KeyError):
                        confirmed = False
                    return self.respond(200 if confirmed else 503, {"accepted": confirmed})
                elif self.path == "/stream":
                    accepted = video_output.submit(payload)
                    # Only an authenticated, fully consumed stream envelope may
                    # reuse its connection. Other routes retain legacy closure.
                    self.stream_keep_alive = (
                        self.headers.get("Connection", "").lower() == "keep-alive")
                elif isinstance(payload, dict) and payload.get("side") in hue_proxies:
                    accepted = hue_proxies[payload["side"]].submit(payload)
                else:
                    accepted = controller.submit(payload)
                self.respond(200, {"accepted": accepted})
            except PowerRequestUnavailable:
                self.respond(503, {"accepted": False})
            except (ValueError, TypeError, KeyError):
                self.respond(400, {"error": "invalid command"})

    return Handler


def main(argv=None, service_info=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--targets", type=Path, required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--port", type=int, default=19880)
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--hue-config", type=Path)
    parser.add_argument("--hue-play", action="store_true")
    parser.add_argument("--hue-stream-config", type=Path)
    parser.add_argument("--native-profile", type=Path)
    parser.add_argument("--power-owner-token-file", type=Path)
    parser.add_argument("--power-authority", choices=("owner", "homekit"), default="owner",
                        help="owner: remote heartbeat; homekit: this adapter executes power")
    args = parser.parse_args(argv)
    if args.hue_play and not args.hue_config:
        parser.error("--hue-play requires --hue-config")
    if args.hue_stream_config and not args.hue_play:
        parser.error("--hue-stream-config requires --hue-play and --hue-config")
    if args.native_profile and not args.hue_config:
        parser.error("--native-profile requires --hue-config")
    if args.hue_config and not args.native_profile:
        parser.error("--hue-config requires --native-profile")
    native_profile = NativeHueProfile.load(args.native_profile) if args.native_profile else None
    state_dir = args.state_dir or args.targets.parent
    if (state_dir / "live-output-hold").exists():
        raise SystemExit("Owner live-output hold is active; physical adapter remains stopped")
    config = json.loads(args.targets.read_text())
    layout_store = LayoutStore(state_dir / "desk-layout.json")
    composition_profile = layout_store.sync_profile()
    for device in config["targets"]:
        known_h6076 = device.get("sku") == "H6076" or device.get("model") == "H6076"
        auto_segments = composition_profile is not None and known_h6076
        if device.get("segment_count", 0) != 7 and (GRADIENT_MODE or auto_segments):
            device["segment_count"] = 7
            if auto_segments:
                device.setdefault("max_gradient_packets_per_second", 2)
        else:
            device.setdefault("segment_count", 0)
    if not args.token_file.exists():
        descriptor = os.open(args.token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(secrets.token_hex(24) + "\n")
    token = args.token_file.read_text().strip()
    power_owner = power_token = homekit = None
    power_requests = None
    preview = OutputPreviewStore()
    sender = lan.send_to_target
    if args.power_owner_token_file:
        power_token = args.power_owner_token_file.read_text().strip()
        if (not 32 <= len(power_token) <= 128
                or any(c not in "0123456789abcdef" for c in power_token)
                or hmac.compare_digest(power_token.encode(), token.encode())):
            raise ValueError("Invalid power owner credential")
        if args.power_authority == "homekit":
            idle = {}
            power_owner = homekit = HomeKitPower(
                config["targets"], state_dir / "homekit-power.json", lan.send_to_target,
                observe=lambda: controller.power_observations(),
                idle=lambda side: idle["check"](side))
            power_requests = HomeKitRequests(homekit)
        else:
            power_owner = PowerOwner(config["targets"])
            power_requests = PowerRequests(power_owner)
        sender = power_owner.wrap(sender)
    elif args.power_authority == "homekit":
        raise ValueError("HomeKit power requires a power owner credential")
    controller = PilotController(config["targets"],
                                 sender=preview.wrap_govee(sender, "scene-output"),
                                 layout_store=layout_store,
                                 power_owner=power_owner,
                                 configuration="scene")
    hue_proxies = {}
    hue_config = None
    if args.hue_config:
        hue_config = json.loads(args.hue_config.read_text())
        hue_proxies["strip"] = HueStripProxy(
            PinnedHueClient(hue_config, profile=native_profile), layout_store=layout_store,
            preview=preview, profile=native_profile,
        )
        if args.hue_play:
            for role, resource in native_profile.play_ids.items():
                hue_proxies[role] = HueStripProxy(
                    PinnedHueClient(hue_config, resource_id=resource, profile=native_profile),
                    role=role,
                    layout_store=layout_store,
                    preview=preview,
                    profile=native_profile,
                )
    video = None
    if args.hue_stream_config:
        stream_config = json.loads(args.hue_stream_config.read_text())
        if stream_config.get("bridge") != hue_config["bridge"]:
            raise ValueError("Stream and scene bridge must match")
        video = VideoOutput(
            PilotController(config["targets"],
                            sender=preview.wrap_govee(sender, "sync-output"),
                            layout_store=layout_store,
                            power_owner=power_owner),
            HueEntertainmentRelay(
                stream_config, layout_store=layout_store, preview=preview, profile=native_profile,
                location_cache_file=state_dir / "native-location-cache.json"),
            hue_proxies.values(),
            diagnostics=StreamDiagnostics(state_dir / 'adapter-stream-health.jsonl'),
        )
        for proxy in hue_proxies.values():
            proxy.output_lock = video.lock
            proxy.write_gate = lambda: not video.active()
    if homekit is not None:
        idle["check"] = lambda side: (
            (video is None or not video.active()) and side not in controller.jobs)
    controller.pause_gate = lambda: (
        (video is None or not video.active())
        and all(proxy.govee_allowed() for proxy in hue_proxies.values() if proxy.included())
    )
    lan.CONFIG = args.targets
    legacy_roles = {("govee", lan.device_key(device)): "govee-" + device["side"]
                    for device in controller.targets}
    hue_layout_roles = {"strip": "hue-monitor", "hue_left": "play-left",
                        "hue_center": "play-center", "hue_right": "play-right"}
    for role, proxy in hue_proxies.items():
        resource = getattr(proxy, "resource_id", None)
        if resource:
            legacy_roles[("hue", resource)] = hue_layout_roles[role]
    catalog = DeviceCatalog(legacy_roles=legacy_roles)
    catalog.provider_state("govee", "discovering")
    catalog.provider_state("hue", "discovering" if hue_config is not None else "not_configured")
    stop = threading.Event()
    server = ThreadingHTTPServer(
        (args.bind, args.port),
        handler_for(
            controller,
            token,
            hue_proxies.get("strip"),
            {role: proxy for role, proxy in hue_proxies.items() if role != "strip"},
            video_output=video,
            power_owner=power_owner,
            power_token=power_token,
            service_info=service_info,
            catalog=catalog,
            preview=preview,
            power_requests=power_requests,
            homekit=homekit,
        ),
    )
    video_thread = http_thread = None
    video_stop = threading.Event()

    def publish_statuses(statuses):
        controller.update_statuses(statuses)
        if video is not None:
            video.controller.update_statuses(statuses)
        if homekit is not None:
            homekit.tick()

    try:
        if hue_config is not None:
            reader = HueDiscoveryReader(hue_config, catalog)
            threading.Thread(target=reader.run, args=(stop,), daemon=True,
                             name="hue-discovery").start()
        for role, proxy in hue_proxies.items():
            threading.Thread(
                target=proxy.run, args=(stop,), daemon=True, name="pilot-" + role
            ).start()
        if video is not None:
            video_thread = threading.Thread(
                target=video.run, args=(video_stop,), name="pilot-video"
            )
            video_thread.start()
        transport = PilotTransport(
            controller.targets,
            publish_statuses,
            remember=lambda: lan.save_config({"targets": controller.targets}),
            discovery=catalog,
        )
        threading.Thread(
            target=transport.run, args=(stop,), daemon=True, name="pilot-status"
        ).start()
        http_thread = threading.Thread(target=server.serve_forever, daemon=True, name="pilot-http")
        http_thread.start()
        print(
            f"Pilot adapter on port {args.port}; two pinned Govee; "
            + ("HomeKit power authority." if homekit is not None else "no power commands."),
            flush=True,
        )
        deadline = time.monotonic()
        while not stop.is_set():
            if video is not None and video.active():
                video.render()
                with controller.lock:
                    controller.last_sent.clear()
            else:
                controller.render()
            deadline += 0.05
            delay = deadline - time.monotonic()
            if delay < 0:
                deadline = time.monotonic()
            stop.wait(max(0, delay))
    except KeyboardInterrupt:
        print("Pilot adapter stopped.", flush=True)
    finally:
        stop.set()
        if power_requests is not None:
            power_requests.close()
        if http_thread is not None and http_thread.is_alive():
            server.shutdown()
        server.server_close()
        if video_thread is not None and video_thread.ident is not None:
            video.lease.close()
            video_stop.set()
            video_thread.join()
            for proxy in hue_proxies.values():
                proxy.process_once()
            with controller.lock:
                controller.last_sent.clear()
            controller.render()


if __name__ == "__main__":
    main()
