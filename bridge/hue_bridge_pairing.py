"""Pairing with the physical Hue bridge for Light Studio: find, link button, keys.

Pairing stores the application key, the streaming client key, the application ID,
the bridge address and the bridge ID in one private file (mode 0600). Nothing
returned by `HueBridgePairing` contains a key or the application ID. Every request
verifies the bridge certificate against the Signify root CA and reads the bridge
ID from it; the link-button request goes only to a bridge whose answer names the
same ID.
"""

import http.client
import json
import os
import re
import ssl
import threading
import time
import uuid
from pathlib import Path

from hue_area import bridge_context, bridge_id
from sync_box import box_address, mdns_search

SERVICE = "_hue._tcp.local"
DEVICE_TYPE = "lights_can_communicate#light_studio"
PAIR_SECONDS = 30
READ_TIMEOUT = 3.0
# The bridge answers a registration with this error until its link button is pressed.
LINK_BUTTON_NOT_PRESSED = 101
FIELDS = ("bridge", "bridge_id", "name", "hue_key", "client_key", "application_id")
_KEY = re.compile(r"[A-Za-z0-9_-]{20,64}\Z")
_CLIENT_KEY = re.compile(r"[0-9A-Fa-f]{32}\Z")
_BRIDGE_ID = re.compile(r"[0-9a-f]{16}\Z")
_MAX_BYTES = 65536


class BridgeError(Exception):
    """The address does not answer as a verified Hue bridge."""


class OtherBridge(Exception):
    """Another bridge is pinned and Hue lights of it are connected."""


class Configured(Exception):
    """The service configuration names its own Hue files, which always win."""


def https_json(ip, method, path, body=None, key=None, timeout=READ_TIMEOUT):
    """One verified HTTPS request; return (status, JSON value, bridge ID, headers).

    The certificate must chain to the Signify root CA; its common name is the bridge
    ID. A key is sent only after that check.
    """
    connection = http.client.HTTPSConnection(ip, 443, timeout=timeout,
                                             context=bridge_context())
    try:
        connection.connect()
        identity = bridge_id(connection.sock.getpeercert())
        headers = {"Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body, separators=(",", ":")).encode()
            headers["Content-Type"] = "application/json"
        if key is not None:
            headers["hue-application-key"] = key
        connection.request(method, path, body=data, headers=headers)
        response = connection.getresponse()
        raw = response.read(_MAX_BYTES + 1)
        if len(raw) > _MAX_BYTES:
            raise BridgeError("Oversize Hue bridge answer")
        try:
            answer = json.loads(raw) if raw.strip() else None
        except ValueError:
            answer = None
        return response.status, answer, identity, {k.lower(): v for k, v in response.getheaders()}
    finally:
        connection.close()


def discover(timeout=1.5, interface=None):
    """Addresses of the Hue bridges that answer an mDNS query on this network."""
    return mdns_search(SERVICE, timeout, interface)


def _uuid(value):
    try:
        return str(uuid.UUID(value)) if isinstance(value, str) and len(value) == 36 else None
    except ValueError:
        return None


class HueBridgePairing:
    """The paired Hue bridge: find, pair after the link button, cancel and status."""

    def __init__(self, path, pin_file, has_hue_lights=lambda: False, transport=https_json,
                 find=discover, clock=time.monotonic, sleep=time.sleep,
                 pair_seconds=PAIR_SECONDS, configured=None):
        self.path, self.pin_file = Path(path), Path(pin_file)
        # The Hue configuration file that the service configuration names, if any.
        self.configured = Path(configured) if configured is not None else None
        if self.configured is not None and self.configured.resolve() == self.path.resolve():
            self.configured = None
        self.has_hue_lights = has_hue_lights
        self.transport, self.find_addresses = transport, find
        self.clock, self.sleep, self.pair_seconds = clock, sleep, pair_seconds
        self.lock = threading.RLock()
        self.pairing = None
        self.last_result = None

    # ------------------------------------------------------------- storage --
    def config(self):
        """The stored pairing, or None when there is none or it is invalid."""
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return None
        if (not isinstance(data, dict) or any(not isinstance(data.get(k), str) for k in FIELDS)
                or not _KEY.match(data["hue_key"]) or not _CLIENT_KEY.match(data["client_key"])
                or _uuid(data["application_id"]) is None
                or not _BRIDGE_ID.match(data["bridge_id"])):
            return None
        return data

    def _write(self, path, text):
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = path.with_name("." + path.name + ".tmp")
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)

    def _pinned(self):
        try:
            value = self.pin_file.read_text().strip()
        except OSError:
            return None
        return value or None

    # ------------------------------------------------------------- devices --
    def describe(self, ip):
        """Name and ID of the verified Hue bridge at `ip`; raise BridgeError otherwise."""
        try:
            status, answer, identity, _ = self.transport(ip, "GET", "/api/0/config")
        except (OSError, ssl.SSLError, http.client.HTTPException) as error:
            raise BridgeError("Hue bridge unreachable") from error
        if (status != 200 or not isinstance(answer, dict)
                or str(answer.get("bridgeid", "")).lower() != identity):
            raise BridgeError("Not a Hue bridge")
        name = answer.get("name")
        return {"ip": ip, "bridge_id": identity,
                "name": name.strip()[:40] if isinstance(name, str) and name.strip()
                else "Hue bridge"}

    def find(self):
        bridges = []
        try:
            addresses = self.find_addresses()
        except OSError:
            addresses = []
        for ip in addresses:
            try:
                info = self.describe(box_address(ip))
            except (ValueError, BridgeError):
                continue
            bridges.append({"ip": info["ip"], "name": info["name"]})
        return {"bridges": bridges}

    # ------------------------------------------------------------- pairing --
    def pair(self, ip):
        """Start the registration loop for the bridge at `ip`; the owner presses its button.

        Raise ValueError for a bad address, BridgeError when it is not a verified Hue
        bridge, OtherBridge when another bridge is pinned and its Hue lights are
        connected.
        """
        if self.configured is not None:
            raise Configured()
        info = self.describe(box_address(ip))
        pinned = self._pinned()
        if pinned is not None and pinned != info["bridge_id"] and self.has_hue_lights():
            raise OtherBridge()
        with self.lock:
            if self.pairing is not None:
                self.pairing["stop"].set()
            attempt = {**info, "deadline": self.clock() + self.pair_seconds,
                       "stop": threading.Event()}
            self.pairing = attempt
            self.last_result = None
        threading.Thread(target=self._register, args=(attempt,), daemon=True).start()
        return self.status()

    def _register(self, attempt):
        body = {"devicetype": DEVICE_TYPE, "generateclientkey": True}
        result = "timeout"
        while not attempt["stop"].is_set() and self.clock() < attempt["deadline"]:
            try:
                status, answer, identity, _ = self.transport(attempt["ip"], "POST", "/api",
                                                             body=body)
            except (OSError, ssl.SSLError, http.client.HTTPException, BridgeError):
                status, answer, identity = 0, None, None
            if identity is not None and identity != attempt["bridge_id"]:
                result = "failed"
                break
            item = answer[0] if isinstance(answer, list) and answer else {}
            success = item.get("success") if isinstance(item, dict) else None
            if status == 200 and isinstance(success, dict):
                result = self._finish(attempt, success)
                break
            self.sleep(1.0)
        with self.lock:
            if self.pairing is attempt:
                self.pairing = None
                if not attempt["stop"].is_set():
                    self.last_result = result

    def _finish(self, attempt, success):
        key, client_key = success.get("username"), success.get("clientkey")
        if (not isinstance(key, str) or not _KEY.match(key)
                or not isinstance(client_key, str) or not _CLIENT_KEY.match(client_key)):
            return "failed"
        try:
            status, _, identity, headers = self.transport(attempt["ip"], "GET", "/auth/v1",
                                                          key=key)
        except (OSError, ssl.SSLError, http.client.HTTPException, BridgeError):
            return "failed"
        application = _uuid(headers.get("hue-application-id"))
        if status != 200 or identity != attempt["bridge_id"] or application is None:
            return "failed"
        with self.lock:
            if attempt["stop"].is_set():
                return "cancelled"
            try:
                self._write(self.path, json.dumps({
                    "bridge": attempt["ip"], "bridge_id": attempt["bridge_id"],
                    "name": attempt["name"], "hue_key": key, "client_key": client_key,
                    "application_id": application}))
                self._write(self.pin_file, attempt["bridge_id"] + "\n")
            except OSError:
                return "failed"
        return "paired"

    def cancel(self):
        with self.lock:
            if self.pairing is not None:
                self.pairing["stop"].set()
                self.pairing = None
        return self.status()

    # -------------------------------------------------------------- status --
    def status(self):
        """The public pairing state; never a key or the application ID."""
        with self.lock:
            if self.pairing is not None:
                return {"state": "pairing", "name": self.pairing["name"],
                        "ip": self.pairing["ip"],
                        "left": max(0, round(self.pairing["deadline"] - self.clock()))}
            if self.configured is not None:
                # Light Studio does not read that file: it holds the service's keys.
                return {"state": "configured"}
            config = self.config()
            result = {"state": "paired", "name": config["name"], "ip": config["bridge"]} \
                if config is not None else {"state": "none"}
            if self.last_result is not None:
                result["result"] = self.last_result
            return result
