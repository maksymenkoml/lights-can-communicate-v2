"""A light's membership in the core's native Entertainment area (#7, #60).

The core owns one Entertainment area on the paired bridge: the one in the light
registry (an existing pairing's area is adopted as the core's), or one that it
creates, named "Light Studio", for the first Hue light. With the paired
credentials this module finds the light's entertainment service and its channels
there, and adds a light to the area or takes it out. The area is changed only
while nothing streams to it. Areas that the core does not own are never changed.
"""

import http.client
import json
import math
import os
import re
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


# The Signify private root CA that signs Hue Bridge certificates ("Using HTTPS" in
# the Hue developer documentation). SHA-256 F0:BD:8E:65:09:E8:2F:77:4D:63:BC:00:9D:
# 53:88:C9:69:FE:3D:CF:7D:6D:54:1D:63:51:B7:2B:89:8D:8A:CF.
SIGNIFY_ROOT_CA = """\
-----BEGIN CERTIFICATE-----
MIICMjCCAdigAwIBAgIUO7FSLbaxikuXAljzVaurLXWmFw4wCgYIKoZIzj0EAwIw
OTELMAkGA1UEBhMCTkwxFDASBgNVBAoMC1BoaWxpcHMgSHVlMRQwEgYDVQQDDAty
b290LWJyaWRnZTAiGA8yMDE3MDEwMTAwMDAwMFoYDzIwMzgwMTE5MDMxNDA3WjA5
MQswCQYDVQQGEwJOTDEUMBIGA1UECgwLUGhpbGlwcyBIdWUxFDASBgNVBAMMC3Jv
b3QtYnJpZGdlMFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEjNw2tx2AplOf9x86
aTdvEcL1FU65QDxziKvBpW9XXSIcibAeQiKxegpq8Exbr9v6LBnYbna2VcaK0G22
jOKkTqOBuTCBtjAPBgNVHRMBAf8EBTADAQH/MA4GA1UdDwEB/wQEAwIBhjAdBgNV
HQ4EFgQUZ2ONTFrDT6o8ItRnKfqWKnHFGmQwdAYDVR0jBG0wa4AUZ2ONTFrDT6o8
ItRnKfqWKnHFGmShPaQ7MDkxCzAJBgNVBAYTAk5MMRQwEgYDVQQKDAtQaGlsaXBz
IEh1ZTEUMBIGA1UEAwwLcm9vdC1icmlkZ2WCFDuxUi22sYpLlwJY81Wrqy11phcO
MAoGCCqGSM49BAMCA0gAMEUCIEBYYEOsa07TH7E5MJnGw557lVkORgit2Rm1h3B2
sFgDAiEA1Fj/C3AN5psFMjo0//mrQebo0eKd3aWRx+pQY08mk48=
-----END CERTIFICATE-----
"""
_BRIDGE_ID = re.compile(r"[0-9a-f]{16}\Z")


AREA_NAME = "Light Studio"


class AreaMissing(OSError):
    """The bridge has no area with the registry's area ID."""


class CertificateChanged(OSError):
    """The bridge certificate does not name the pinned bridge."""


def _check_pin(path, bridge_id):
    """Bind the area client to one bridge.

    The certificate chain is verified against the Signify root CA, and its common
    name is the bridge id. The first verified request records that id; every later
    request must present a certificate for the same bridge before the application
    key is sent. After a bridge replacement, delete the pin file.
    """
    path = Path(path)
    try:
        pinned = path.read_text().strip()
    except FileNotFoundError:
        pinned = None
    if pinned is not None:
        if not secrets.compare_digest(pinned, bridge_id):
            raise CertificateChanged("Unexpected Hue bridge")
        return
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=".hue-pin-", suffix=".tmp",
                                     delete=False) as stream:
        stream.write(bridge_id + "\n")
    os.chmod(stream.name, 0o600)
    os.replace(stream.name, path)


def bridge_context():
    """A TLS context that accepts only certificates issued by the Signify root CA.

    Bridge certificates name the bridge id instead of its address, so the host name
    is not checked here; the caller compares the common name with the pinned id.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_verify_locations(cadata=SIGNIFY_ROOT_CA)
    return context


def bridge_id(certificate):
    """Return the bridge id that a verified certificate names, or raise OSError."""
    subject = dict(item[0] for item in (certificate or {}).get("subject", ()) if item)
    name = subject.get("commonName")
    if not isinstance(name, str) or _BRIDGE_ID.match(name.lower()) is None:
        raise OSError("Hue bridge certificate names no bridge")
    return name.lower()


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
        # None until the core creates its area for the first Hue light.
        self.area_id = _uuid(area_id) if area_id is not None else None
        if area_id is not None and self.area_id is None:
            raise ValueError("Invalid paired Entertainment area")
        # The area this object created and no enrollment has kept yet.
        self._created = None
        if requester is None and pin_file is None:
            raise ValueError("A pinned Hue bridge certificate file is required")
        self._pin_file = pin_file
        self._requester = requester or self._request

    def _request(self, path, method="GET", body=None):
        connection = http.client.HTTPSConnection(self._bridge, 443, timeout=self._timeout,
                                                 context=bridge_context())
        deadline = time.monotonic() + self._timeout
        try:
            connection.connect()
            # The application key is sent only to the verified, pinned bridge.
            _check_pin(self._pin_file, bridge_id(connection.sock.getpeercert()))
            headers = {"hue-application-key": self._key, "Accept": "application/json"}
            data = None
            if body is not None:
                data = json.dumps(body, separators=(",", ":")).encode()
                headers["Content-Type"] = "application/json"
            connection.request(method, path, body=data, headers=headers)
            response = connection.getresponse()
            if response.status == 404 and method == "GET" and self.area_id is not None \
                    and path == self._path:
                raise AreaMissing("Hue area not found")
            # A created resource answers 201 (verified on a Hue Bridge on 2026-11-24).
            if response.status not in (200, 201):
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

    def _create(self, light):
        """Create the core's area with ``light`` as its only member."""
        service = self._service(light)
        try:
            created = self._data("/clip/v2/resource/entertainment_configuration", "POST", {
                "type": "entertainment_configuration",
                "metadata": {"name": AREA_NAME},
                "configuration_type": "screen",
                "locations": {"service_locations": [
                    {"service": {"rid": service, "rtype": "entertainment"},
                     "positions": [{"x": 0, "y": 0, "z": 0}]}]},
            })
        except (OSError, ValueError):
            self._remove_uncertain(service)
            raise
        rid = _uuid(created[0].get("rid")) if len(created) == 1 and isinstance(
            created[0], dict) else None
        if rid is None:
            raise ValueError("Invalid Hue area response")
        previous, self.area_id = self.area_id, rid
        try:
            area = self._area()
            if not self._contains(area, light):
                raise ValueError("Hue light did not join the area")
            segments = self._segments(area, service)
        except BaseException:
            self._delete(rid)
            self.area_id = previous
            raise
        self._created = rid
        return {"service": service, "segments": segments}, True

    def _remove_uncertain(self, service):
        """After a creation with an unknown result, delete an area that it may have
        left: named Light Studio, with this light's service as its only member."""
        try:
            areas = self._data("/clip/v2/resource/entertainment_configuration")
        except (OSError, ValueError):
            return
        for area in areas:
            rows = (area.get("locations") or {}).get("service_locations") \
                if isinstance(area, dict) else None
            rid = _uuid(area.get("id")) if isinstance(area, dict) else None
            if (rid is not None and rid != self.area_id
                    and (area.get("metadata") or {}).get("name") == AREA_NAME
                    and isinstance(rows, list) and len(rows) == 1 and isinstance(rows[0], dict)
                    and _uuid((rows[0].get("service") or {}).get("rid")) == service):
                self._delete(rid)

    def _delete(self, rid):
        try:
            self._data("/clip/v2/resource/entertainment_configuration/" + rid, "DELETE")
        except (OSError, ValueError):
            pass  # An empty area named Light Studio stays; the owner can delete it.

    def abandon(self, light):
        """Undo a ``join`` whose enrollment failed: delete an area created for it,
        else take the light out again."""
        if self._created is not None and self._created == self.area_id:
            rid, self._created = self._created, None
            self._delete(rid)
            self.area_id = None
            return
        self.leave(light)

    def join(self, light, replace_missing=False):
        """Add native ``light`` to the inactive area; return
        ``({"service", "segments"}, added)``. ``added`` is False when the light
        was already a member. Without an area, the core creates its own with this
        light. With ``replace_missing`` (no Hue light depends on the area), an area
        that the bridge no longer has is replaced the same way. Raise AreaBusy
        while the area streams, AreaShared when the light's channels are shared;
        the area is then unchanged."""
        light = _uuid(light)
        if light is None:
            raise ValueError("Invalid Hue light")
        self._created = None
        if self.area_id is None:
            return self._create(light)
        try:
            area = self._inactive()
        except AreaMissing:
            if not replace_missing:
                raise
            return self._create(light)
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
        if self.area_id is None:
            return None
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
        if (before is not None and self.area_id is not None
                and self._area().get("status") == "inactive"):
            self._restore(before)
