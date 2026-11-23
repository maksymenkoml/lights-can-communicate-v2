"""Read one light's channels in the paired native Entertainment area (#7 stage 5b).

Before the setup flow (#60) chooses the area, a new Hue light is enrolled from the
physical area that the existing pairing already uses: the owner adds the light to
that area in the Hue app, and this reader finds the light's entertainment service
and its channels there with the paired credentials. It only reads.
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


class HueAreaReader:
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

    def _request(self, path):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context),
            _NoRedirect(),
        )
        request = urllib.request.Request(
            "https://" + self._bridge + path, method="GET",
            headers={"hue-application-key": self._key, "Accept": "application/json"},
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

    def _data(self, path):
        response = self._requester(path)
        if (not isinstance(response, dict) or response.get("errors") not in ([], None)
                or not isinstance(response.get("data"), list)):
            raise ValueError("Invalid Hue area response")
        return response["data"]

    def member(self, light):
        """Return ``{"service", "segments"}`` of native ``light`` in the area, or None.

        None means that the light is not a member of the paired area. A response
        that cannot be read raises OSError or ValueError.
        """
        light = _uuid(light)
        if light is None:
            return None
        areas = self._data("/clip/v2/resource/entertainment_configuration/" + self.area_id)
        if len(areas) != 1 or not isinstance(areas[0], dict) or areas[0].get("id") != self.area_id:
            raise ValueError("Invalid Hue area response")
        area = areas[0]
        rows = area.get("light_services")
        if not isinstance(rows, list):
            raise ValueError("Invalid Hue area response")
        if not any(isinstance(row, dict) and row.get("rtype") == "light"
                   and _uuid(row.get("rid")) == light for row in rows):
            return None
        services = [item.get("id") for item in self._data("/clip/v2/resource/entertainment")
                    if isinstance(item, dict)
                    and isinstance(item.get("renderer_reference"), dict)
                    and item["renderer_reference"].get("rtype") == "light"
                    and _uuid(item["renderer_reference"].get("rid")) == light]
        service = _uuid(services[0]) if len(services) == 1 else None
        if service is None:
            raise ValueError("Invalid Hue entertainment service")
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
        return {"service": service, "segments": len(indexes)}
