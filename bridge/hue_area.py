"""A light's membership in the paired native Entertainment area (#7 stage 5b).

Before the setup flow (#60) chooses the area, a new Hue light is enrolled into
the physical area that the existing pairing already uses. With the paired
credentials this module finds the light's entertainment service and its channels
there, and adds a light to the area or takes it out. The area is changed only
while nothing streams to it.
"""

import http.client
import json
import ssl
import time
import urllib.error
import urllib.request
import uuid

import lights_can_communicate_v2 as lan

_MAX_BYTES = 1024 * 1024


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _uuid(value):
    if not isinstance(value, str) or len(value) != 36:
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None


class AreaBusy(Exception):
    """The area streams now, so its membership is not changed."""


class HueArea:
    """No constructor IO; errors never include credentials or addresses."""

    def __init__(self, config, area_id, requester=None, timeout=1.5):
        key = config.get("hue_key") if isinstance(config, dict) else None
        if not isinstance(key, str) or not 1 <= len(key) <= 256:
            raise ValueError("Invalid paired Hue configuration")
        self._bridge = lan.local_ip(config["bridge"])
        self._key, self._timeout = key, timeout
        self.area_id = _uuid(area_id)
        if self.area_id is None:
            raise ValueError("Invalid paired Entertainment area")
        self._requester = requester or self._request

    def _request(self, path, method="GET", body=None):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context),
            _NoRedirect(),
        )
        headers = {"hue-application-key": self._key, "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body, separators=(",", ":")).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            "https://" + self._bridge + path, data=data, method=method, headers=headers,
        )
        deadline = time.monotonic() + self._timeout
        try:
            response = opener.open(request, timeout=self._timeout)
        except urllib.error.HTTPError as error:
            error.close()
            raise OSError("Hue area request failed") from None
        try:
            with response:
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
            for member in members:
                reference = member.get("service") if isinstance(member, dict) else None
                if isinstance(reference, dict) and _uuid(reference.get("rid")) == service:
                    indexes.append(member.get("index"))
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

    def member(self, light):
        """Return ``{"service", "segments"}`` of native ``light`` in the area, or None.

        None means that the light is not a member of the paired area. A response
        that cannot be read raises OSError or ValueError.
        """
        light = _uuid(light)
        if light is None:
            return None
        area = self._area()
        if not self._contains(area, light):
            return None
        service = self._service(light)
        return {"service": service, "segments": self._segments(area, service)}

    def join(self, light):
        """Add native ``light`` to the inactive area at its center; return
        ``({"service", "segments"}, added)``. ``added`` is False when the light
        was already a member. Raise AreaBusy while the area streams."""
        light = _uuid(light)
        if light is None:
            raise ValueError("Invalid Hue light")
        area = self._inactive()
        if self._contains(area, light):
            service = self._service(light)
            return {"service": service, "segments": self._segments(area, service)}, False
        service = self._service(light)
        rows = self._locations(area)
        rows.append({"service": {"rid": service, "rtype": "entertainment"},
                     "positions": [{"x": 0, "y": 0, "z": 0}]})
        self._write(rows)
        member = self.member(light)
        if member is None or member["service"] != service:
            raise ValueError("Hue light did not join the area")
        return member, True

    def leave(self, light):
        """Take native ``light`` out of the inactive area; a non-member is a no-op.
        Raise AreaBusy while the area streams."""
        light = _uuid(light)
        if light is None:
            raise ValueError("Invalid Hue light")
        area = self._inactive()
        if not self._contains(area, light):
            return
        service = self._service(light)
        self._write([row for row in self._locations(area)
                     if _uuid(row["service"].get("rid")) != service])
        if self._contains(self._area(), light):
            raise ValueError("Hue light did not leave the area")
