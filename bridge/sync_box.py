"""Hue Sync Box control for Light Studio: discovery, pairing and Start/Stop Sync.

The access token and the pinned certificate stay in a 0600 file in the private
state directory. Nothing returned by `SyncBox` contains either of them.
"""

import base64
import hashlib
import http.client
import ipaddress
import json
import math
import os
import secrets
import socket
import ssl
import struct
import threading
import time
from pathlib import Path

APP_NAME = "lightscan"
INSTANCE_NAME = "Light Studio"
PAIR_SECONDS = 30
# The box answers a registration with this code until its button is held.
BUTTON_NOT_PRESSED = 16
SERVICE = "_huesync._tcp.local"
MDNS_GROUP = ("224.0.0.251", 5353)
STATUS_TTL = 1.0
RELOCATE_INTERVAL = 30.0
# The box can take several seconds to answer, longer while Sync starts or stops.
READ_TIMEOUT = 10.0
CHANGE_TIMEOUT = 15.0
# A box that answered recently is shown as it was until it stays silent this long.
OFFLINE_GRACE = 15.0
FIELDS = ("ip", "uniqueId", "name", "registrationId", "accessToken", "fingerprint")


class BoxError(Exception):
    """The box did not answer as a Sync Box."""


class CertificateMismatch(BoxError):
    """The device at the address does not present the pinned certificate."""


def box_address(value):
    """Return a private IPv4 address string or raise ValueError."""
    if not isinstance(value, str) or len(value.strip()) > 15:
        raise ValueError("Invalid address")
    address = ipaddress.ip_address(value.strip())
    if address.version != 4 or not address.is_private or address.is_loopback:
        raise ValueError("Address must be on the local network")
    return str(address)


def https_json(ip, method, path, body=None, token=None, pin=None, timeout=READ_TIMEOUT):
    """One HTTPS request to the box; return (status, JSON object, certificate SHA-256).

    The box certificate is self-signed, so trust comes from the pin taken when the
    box was connected: a request with `pin` refuses any other certificate before it
    sends the token.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    connection = http.client.HTTPSConnection(ip, 443, timeout=timeout, context=context)
    try:
        connection.connect()
        certificate = connection.sock.getpeercert(binary_form=True)
        if not certificate:
            raise BoxError("No Sync Box certificate")
        fingerprint = hashlib.sha256(certificate).hexdigest()
        if pin is not None and not secrets.compare_digest(fingerprint, pin):
            raise CertificateMismatch("Unexpected Sync Box certificate")
        headers = {"Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        connection.request(method, path, body=data, headers=headers)
        response = connection.getresponse()
        raw = response.read(65537)
        if len(raw) > 65536:
            raise BoxError("Oversize Sync Box answer")
        try:
            answer = json.loads(raw) if raw.strip() else {}
        except ValueError:
            answer = {}
        return response.status, answer if isinstance(answer, dict) else {}, fingerprint
    finally:
        connection.close()


def _encode_name(name):
    out = b""
    for label in name.split("."):
        out += bytes([len(label)]) + label.encode()
    return out + b"\0"


def mdns_query(service=SERVICE):
    """A one-question PTR query; a query from a port other than 5353 gets unicast answers."""
    header = struct.pack(">HHHHHH", 0, 0, 1, 0, 0, 0)
    return header + _encode_name(service) + struct.pack(">HH", 12, 1)


def _read_name(packet, offset):
    labels, jumps, end = [], 0, None
    while True:
        if offset >= len(packet):
            raise ValueError("Truncated name")
        length = packet[offset]
        if length & 0xC0 == 0xC0:
            if offset + 1 >= len(packet) or jumps > 16:
                raise ValueError("Bad name pointer")
            if end is None:
                end = offset + 2
            offset = ((length & 0x3F) << 8) | packet[offset + 1]
            jumps += 1
            continue
        if length == 0:
            return ".".join(labels), (end if end is not None else offset + 1)
        labels.append(packet[offset + 1:offset + 1 + length].decode("utf-8", "replace"))
        offset += 1 + length


def mdns_answers_service(packet, service=SERVICE):
    """True when a DNS response names the service in any record or PTR target."""
    try:
        if len(packet) < 12:
            return False
        _, flags, questions, answers, authority, additional = struct.unpack(">HHHHHH", packet[:12])
        if not flags & 0x8000:
            return False
        offset = 12
        for _ in range(questions):
            _, offset = _read_name(packet, offset)
            offset += 4
        wanted = service.lower()
        for _ in range(answers + authority + additional):
            name, offset = _read_name(packet, offset)
            kind, _, _, size = struct.unpack(">HHIH", packet[offset:offset + 10])
            offset += 10
            target = _read_name(packet, offset)[0] if kind == 12 else ""
            if name.lower().endswith(wanted) or target.lower().endswith(wanted):
                return True
            offset += size
    except (ValueError, struct.error):
        return False
    return False


def discover(timeout=1.5):
    """Addresses of the Sync Boxes that answer an mDNS query on this network."""
    found = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as sock:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
        sock.bind(("", 0))
        sock.sendto(mdns_query(), MDNS_GROUP)
        end = time.monotonic() + timeout
        while (left := end - time.monotonic()) > 0:
            sock.settimeout(left)
            try:
                packet, (ip, _) = sock.recvfrom(9000)
            except (socket.timeout, OSError):
                break
            if ip not in found and mdns_answers_service(packet):
                found.append(ip)
    return found


class SyncBox:
    """One paired Sync Box: status, Start/Stop, connect and forget."""

    def __init__(self, path, transport=https_json, find=discover, clock=time.monotonic,
                 sleep=time.sleep, pair_seconds=PAIR_SECONDS, background=True):
        self.path = Path(path)
        self.transport = transport
        self.find_addresses = find
        self.clock = clock
        self.sleep = sleep
        self.pair_seconds = pair_seconds
        self.lock = threading.RLock()
        self.pairing = None
        self.background = background
        # (time, public state) of the last read; polls answer from it while a read runs.
        self.cached = None
        self.reading = False
        self.answered_at = None
        self.changing = None
        self.relocated_at = None
        # Bumped when a pairing or Forget changes the stored box; a read that
        # started before must not publish its result.
        self.generation = 0

    # ------------------------------------------------------------- storage --
    def config(self):
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or any(not isinstance(data.get(k), str) for k in FIELDS):
            return None
        return data

    def _save(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            json.dump({k: data[k] for k in FIELDS}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)

    # ------------------------------------------------------------- devices --
    def describe(self, ip, pin=None):
        """Name, id and certificate of the Sync Box at `ip`; raise BoxError if it is not one."""
        try:
            status, device, fingerprint = self.transport(ip, "GET", "/api/v1/device", pin=pin)
        except CertificateMismatch:
            raise
        except (OSError, ssl.SSLError, http.client.HTTPException) as error:
            raise BoxError("Sync Box unreachable") from error
        name, unique = device.get("name"), device.get("uniqueId")
        if (status != 200 or not str(device.get("deviceType", "")).startswith("HSB")
                or not isinstance(unique, str) or not unique):
            raise BoxError("Not a Sync Box")
        return {"ip": ip, "uniqueId": unique,
                "name": name if isinstance(name, str) and name.strip() else "Sync Box",
                "fingerprint": fingerprint}

    def find(self):
        boxes = []
        for ip in self.find_addresses():
            try:
                info = self.describe(box_address(ip))
            except (ValueError, BoxError):
                continue
            boxes.append({"ip": info["ip"], "name": info["name"]})
        return {"boxes": boxes}

    # ------------------------------------------------------------- pairing --
    def pair(self, ip):
        """Start the registration loop for the box at `ip`; the owner holds its button."""
        ip = box_address(ip)
        info = self.describe(ip)
        with self.lock:
            if self.pairing is not None:
                self.pairing["stop"].set()
            attempt = {**info, "deadline": self.clock() + self.pair_seconds,
                       "stop": threading.Event(), "result": None}
            self.pairing = attempt
        threading.Thread(target=self._register, args=(attempt,), daemon=True).start()
        return self.status()

    def _register(self, attempt):
        body = {"appName": APP_NAME, "instanceName": INSTANCE_NAME,
                "appSecret": base64.b64encode(secrets.token_bytes(32)).decode()}
        while not attempt["stop"].is_set() and self.clock() < attempt["deadline"]:
            try:
                status, answer, _ = self.transport(attempt["ip"], "POST", "/api/v1/registrations",
                                                   body=body, pin=attempt["fingerprint"])
            except CertificateMismatch:
                break
            except (OSError, ssl.SSLError, http.client.HTTPException, BoxError):
                status, answer = 0, {}
            token, registration = answer.get("accessToken"), answer.get("registrationId")
            if status == 200 and isinstance(token, str) and token:
                with self.lock:
                    if attempt["stop"].is_set():
                        return
                    try:
                        self._save({**attempt, "accessToken": token,
                                    "registrationId": str(registration)})
                    except OSError:
                        # The token cannot be kept: end the attempt with an error.
                        attempt["result"] = "failed"
                        return
                    attempt["result"] = "connected"
                    self.pairing = None
                    self.generation += 1
                    self.cached = None
                return
            self.sleep(1.0)
        with self.lock:
            if not attempt["stop"].is_set():
                attempt["result"] = "timeout"

    def cancel(self):
        with self.lock:
            if self.pairing is not None:
                self.pairing["stop"].set()
                self.pairing = None
        return self.status()

    # -------------------------------------------------------------- status --
    def _request(self, config, method, path, body=None, timeout=READ_TIMEOUT):
        """An authorised request that follows a box that moved to another address."""
        try:
            return self.transport(config["ip"], method, path, body=body, timeout=timeout,
                                  token=config["accessToken"], pin=config["fingerprint"])
        except (OSError, ssl.SSLError, http.client.HTTPException, BoxError):
            moved = self._relocate(config)
            if moved is None:
                raise
            return self.transport(moved["ip"], method, path, body=body, timeout=timeout,
                                  token=moved["accessToken"], pin=moved["fingerprint"])

    def _relocate(self, config):
        now = self.clock()
        if self.relocated_at is not None and now - self.relocated_at < RELOCATE_INTERVAL:
            return None
        self.relocated_at = now
        try:
            addresses = self.find_addresses()
        except OSError:
            return None
        for ip in addresses:
            if ip == config["ip"]:
                continue
            try:
                info = self.describe(box_address(ip), pin=config["fingerprint"])
            except (ValueError, BoxError):
                continue
            if info["uniqueId"] == config["uniqueId"]:
                moved = {**config, "ip": info["ip"]}
                self._save(moved)
                return moved
        return None

    def status(self):
        with self.lock:
            attempt = self.pairing
            if attempt is not None:
                public = {"name": attempt["name"], "ip": attempt["ip"]}
                if attempt["result"] in ("timeout", "failed"):
                    return {"state": attempt["result"], **public}
                if attempt["result"] is None:
                    left = max(0, math.ceil(attempt["deadline"] - self.clock()))
                    return {"state": "connecting", "remaining": left, **public}
            cached = self.cached
            stale = cached is None or self.clock() - cached[0] >= STATUS_TTL
            if stale and cached is not None and self.background:
                # A slow box must not hold every poll: answer from the last read.
                if not self.reading:
                    self.reading = True
                    threading.Thread(target=self._refresh, daemon=True).start()
                stale = False
        if stale:
            self._refresh()
        with self.lock:
            if self.cached is None:
                # Forgotten or connected while this poll read the old box.
                return self.status()
            result = dict(self.cached[1])
            if self.changing is not None and result["state"] == "ready":
                result["changing"] = self.changing
            return result

    def _refresh(self):
        with self.lock:
            generation = self.generation
        try:
            result = self._read_status()
        finally:
            with self.lock:
                self.reading = False
        with self.lock:
            if generation != self.generation:
                return
            now = self.clock()
            if result["state"] in ("ready", "unauthorized", "none"):
                self.answered_at = now
            elif (self.cached is not None and self.cached[1]["state"] == "ready"
                  and self.answered_at is not None and now - self.answered_at < OFFLINE_GRACE):
                # One slow or missed answer: keep showing the box as it was.
                result = self.cached[1]
            self.cached = (now, result)

    def _read_status(self):
        config = self.config()
        if config is None:
            return {"state": "none"}
        try:
            status, execution, _ = self._request(config, "GET", "/api/v1/execution")
        except (OSError, ssl.SSLError, http.client.HTTPException, BoxError):
            config = self.config() or config
            return {"state": "offline", "name": config["name"], "ip": config["ip"]}
        config = self.config() or config
        public = {"name": config["name"], "ip": config["ip"]}
        if status in (401, 403):
            return {"state": "unauthorized", **public}
        if status != 200:
            return {"state": "offline", **public}
        return {"state": "ready", "syncing": execution.get("syncActive") is True, **public}

    def set_sync(self, active):
        """Start or stop Sync; return (HTTP status, public status).

        Polls during the change answer from the last read with `changing`, so every
        page shows Starting or Stopping until the box has answered.
        """
        with self.lock:
            config = self.config()
            if config is None:
                return 409, {"state": "none"}
            if self.changing is not None:
                return 409, self.status()
            self.changing = bool(active)
            generation = self.generation
        try:
            status, _, _ = self._request(config, "PUT", "/api/v1/execution",
                                         body={"syncActive": bool(active)},
                                         timeout=CHANGE_TIMEOUT)
        except (OSError, ssl.SSLError, http.client.HTTPException, BoxError):
            status = 0
        with self.lock:
            self.changing = None
            if status == 200 and generation == self.generation:
                config = self.config() or config
                now = self.clock()
                self.answered_at = now
                self.cached = (now, {"state": "ready", "syncing": bool(active),
                                     "name": config["name"], "ip": config["ip"]})
                return 200, self.status()
            self.cached = None
        result = self.status()
        return (409 if result["state"] == "unauthorized" else 503), result

    def forget(self):
        """Remove the registration on the box when it answers, and always locally."""
        with self.lock:
            if self.pairing is not None:
                self.pairing["stop"].set()
                self.pairing = None
            config = self.config()
            if config is not None and config["registrationId"].isdigit():
                try:
                    self.transport(config["ip"], "DELETE",
                                   "/api/v1/registrations/" + config["registrationId"],
                                   token=config["accessToken"], pin=config["fingerprint"])
                except (OSError, ssl.SSLError, http.client.HTTPException, BoxError):
                    pass
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            self.generation += 1
            self.cached = None
            return self.status()
