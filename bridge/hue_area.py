"""A light's membership in the paired native Entertainment area (#7 stage 5b).

Before the setup flow (#60) chooses the area, a new Hue light is enrolled into
the physical area that the existing pairing already uses. With the paired
credentials this module finds the light's entertainment service and its channels
there, and adds a light to the area or takes it out. The area is changed only
while nothing streams to it.
"""

import hashlib
import http.client
import json
import math
import os
import secrets
import ssl
import tempfile
import time
import uuid
from pathlib import Path

import lights_can_communicate_v2 as lan

_MAX_BYTES = 1024 * 1024


# Where a light with one channel is placed: the bridge merges lights at one position
# into one channel, so a new light takes the free point farthest from all channels.
_CANDIDATES = [{"x": x, "y": y, "z": z} for x in (-1, -0.5, 0, 0.5, 1)
               for y in (-1, 0, 1) for z in (-1, 0, 1)]


def _free_position(area):
    taken = []
    for channel in area.get("channels") or ():
        position = channel.get("position") if isinstance(channel, dict) else None
        if isinstance(position, dict) and all(
                type(position.get(axis)) in (int, float) and math.isfinite(position[axis])
                for axis in "xyz"):
            taken.append(position)
    if not taken:
        return {"x": 0, "y": 0, "z": 0}
    return dict(max(_CANDIDATES, key=lambda point: min(
        sum((point[axis] - other[axis]) ** 2 for axis in "xyz") for other in taken)))


class CertificateChanged(OSError):
    """The bridge presented another certificate than the pinned one."""


def _check_pin(path, fingerprint):
    """Trust the bridge certificate seen first and refuse any other one later.

    The bridge certificate is not checked against a public CA, so this client pins
    it, like the Sync Box client: the first area request records its SHA-256 and
    every later request compares before it sends the application key. After a
    genuine bridge replacement, delete the pin file to trust the new certificate.
    """
    path = Path(path)
    try:
        pinned = path.read_text().strip()
    except FileNotFoundError:
        pinned = None
    if pinned is not None:
        if not secrets.compare_digest(pinned, fingerprint):
            raise CertificateChanged("Unexpected Hue bridge certificate")
        return
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=".hue-pin-", suffix=".tmp",
                                     delete=False) as stream:
        stream.write(fingerprint + "\n")
    os.chmod(stream.name, 0o600)
    os.replace(stream.name, path)


def _uuid(value):
    if not isinstance(value, str) or len(value) != 36:
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None


class AreaBusy(Exception):
    """The area streams now, so its membership is not changed."""


class AreaShared(Exception):
    """The bridge gave the light channels that other lights share, so the light
    cannot be driven on its own. A second gradient light shares the first one's
    channels in every area type."""


class HueArea:
    """No constructor IO; errors never include credentials or addresses."""

    def __init__(self, config, area_id, requester=None, timeout=1.5, pin_file=None):
        key = config.get("hue_key") if isinstance(config, dict) else None
        if not isinstance(key, str) or not 1 <= len(key) <= 256:
            raise ValueError("Invalid paired Hue configuration")
        self._bridge = lan.local_ip(config["bridge"])
        self._key, self._timeout = key, timeout
        self.area_id = _uuid(area_id)
        if self.area_id is None:
            raise ValueError("Invalid paired Entertainment area")
        if requester is None and pin_file is None:
            raise ValueError("A pinned Hue bridge certificate file is required")
        self._pin_file = pin_file
        self._requester = requester or self._request

    def _request(self, path, method="GET", body=None):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE  # Trust comes from the pin (see _check_pin).
        connection = http.client.HTTPSConnection(self._bridge, 443, timeout=self._timeout,
                                                 context=context)
        deadline = time.monotonic() + self._timeout
        try:
            connection.connect()
            certificate = connection.sock.getpeercert(binary_form=True)
            if not certificate:
                raise OSError("No Hue bridge certificate")
            # The application key is sent only after the certificate matched.
            _check_pin(self._pin_file, hashlib.sha256(certificate).hexdigest())
            headers = {"hue-application-key": self._key, "Accept": "application/json"}
            data = None
            if body is not None:
                data = json.dumps(body, separators=(",", ":")).encode()
                headers["Content-Type"] = "application/json"
            connection.request(method, path, body=data, headers=headers)
            response = connection.getresponse()
            if response.status != 200:
                raise OSError("Hue area request failed")
            raw = bytearray()
            while len(raw) <= _MAX_BYTES:
                if time.monotonic() >= deadline:
                    raise OSError("Hue area response timed out")
                chunk = response.read1(min(65536, _MAX_BYTES + 1 - len(raw)))
                if not chunk:
                    break
                raw.extend(chunk)
            if len(raw) > _MAX_BYTES:
                raise ValueError("Hue area response too large")
            return json.loads(raw)
        except (http.client.HTTPException, RecursionError, UnicodeDecodeError):
            raise ValueError("Invalid Hue area response") from None
        finally:
            connection.close()

    def _data(self, path, method="GET", body=None):
        response = self._requester(path, method, body)
        if (not isinstance(response, dict) or response.get("errors") not in ([], None)
                or not isinstance(response.get("data"), list)):
            raise ValueError("Invalid Hue area response")
        return response["data"]

    def _area(self):
        areas = self._data(self._path)
        if len(areas) != 1 or not isinstance(areas[0], dict) or areas[0].get("id") != self.area_id:
            raise ValueError("Invalid Hue area response")
        area = areas[0]
        if not isinstance(area.get("light_services"), list):
            raise ValueError("Invalid Hue area response")
        return area

    @staticmethod
    def _contains(area, light):
        return any(isinstance(row, dict) and row.get("rtype") == "light"
                   and _uuid(row.get("rid")) == light for row in area["light_services"])

    def _service(self, light):
        services = [item.get("id") for item in self._data("/clip/v2/resource/entertainment")
                    if isinstance(item, dict)
                    and isinstance(item.get("renderer_reference"), dict)
                    and item["renderer_reference"].get("rtype") == "light"
                    and _uuid(item["renderer_reference"].get("rid")) == light]
        service = _uuid(services[0]) if len(services) == 1 else None
        if service is None:
            raise ValueError("Invalid Hue entertainment service")
        return service

    @staticmethod
    def _segments(area, service):
        channels = area.get("channels")
        if not isinstance(channels, list):
            raise ValueError("Invalid Hue area response")
        indexes = []
        for channel in channels:
            members = channel.get("members") if isinstance(channel, dict) else None
            if not isinstance(members, list):
                raise ValueError("Invalid Hue area response")
            mine = [member for member in members
                    if isinstance(member, dict) and isinstance(member.get("service"), dict)
                    and _uuid(member["service"].get("rid")) == service]
            if mine and len(members) != 1:
                raise AreaShared()
            indexes += [member.get("index") for member in mine]
        if (not indexes or any(type(index) is not int for index in indexes)
                or sorted(indexes) != list(range(len(indexes)))):
            raise ValueError("Invalid Hue channel layout")
        return len(indexes)

    @staticmethod
    def _locations(area):
        locations = area.get("locations")
        rows = locations.get("service_locations") if isinstance(locations, dict) else None
        if not isinstance(rows, list) or any(
                not isinstance(row, dict) or not isinstance(row.get("service"), dict)
                or _uuid(row["service"].get("rid")) is None
                or not isinstance(row.get("positions"), list) for row in rows):
            raise ValueError("Invalid Hue area locations")
        # Only the writable fields go back to the bridge, unchanged.
        return [{key: row[key] for key in ("service", "positions", "equalization_factor")
                 if key in row} for row in rows]

    def _inactive(self):
        area = self._area()
        if area.get("status") != "inactive":
            raise AreaBusy()
        return area

    def _write(self, rows):
        self._data(self._path, "PUT", {"locations": {"service_locations": rows}})

    @property
    def _path(self):
        return "/clip/v2/resource/entertainment_configuration/" + self.area_id

    def _restore(self, rows):
        # Writing the earlier locations again is safe whether or not the failed write
        # was applied; the bridge then rebuilds the other lights' channels.
        try:
            self._write(rows)
        except (OSError, ValueError):
            pass  # The relay refuses the changed area until it is repaired.

    def join(self, light):
        """Add native ``light`` to the inactive area; return
        ``({"service", "segments"}, added)``. ``added`` is False when the light
        was already a member. Raise AreaBusy while the area streams, AreaShared
        when the light's channels are shared; the area is then unchanged."""
        light = _uuid(light)
        if light is None:
            raise ValueError("Invalid Hue light")
        area = self._inactive()
        if self._contains(area, light):
            service = self._service(light)
            return {"service": service, "segments": self._segments(area, service)}, False
        service = self._service(light)
        before = self._locations(area)
        try:
            self._write(before + [{"service": {"rid": service, "rtype": "entertainment"},
                                   "positions": [_free_position(area)]}])
            changed = self._area()
            if not self._contains(changed, light):
                raise ValueError("Hue light did not join the area")
            if any(isinstance(channel, dict) and isinstance(channel.get("members"), list)
                   and len(channel["members"]) != 1 for channel in changed.get("channels", [])):
                raise AreaShared()
            return {"service": service, "segments": self._segments(changed, service)}, True
        except BaseException:
            self._restore(before)
            raise

    def leave(self, light):
        """Take native ``light`` out of the inactive area. Return the earlier
        locations for ``rejoin``, or None when the light was not a member. Raise
        AreaBusy while the area streams; a failed change restores the area."""
        light = _uuid(light)
        if light is None:
            raise ValueError("Invalid Hue light")
        area = self._inactive()
        if not self._contains(area, light):
            return None
        service = self._service(light)
        before = self._locations(area)
        try:
            self._write([row for row in before if _uuid(row["service"].get("rid")) != service])
            if self._contains(self._area(), light):
                raise ValueError("Hue light did not leave the area")
        except BaseException:
            self._restore(before)
            raise
        return before

    def rejoin(self, before):
        """Undo ``leave`` with the locations it returned, if the area is inactive."""
        if before is not None and self._area().get("status") == "inactive":
            self._restore(before)
