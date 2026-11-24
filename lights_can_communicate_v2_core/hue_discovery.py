"""Bounded read-only discovery using the already paired local Hue Bridge."""

import http.client
import json
import re
import time

from .discovery import _finite_positive, _local_ip

_PATHS = tuple("/clip/v2/resource/" + resource for resource in ("bridge", "device", "light"))
_MAX_BYTES = 1024 * 1024


class HueDiscoveryReader:
    """No constructor IO. The owner controls the background worker and stop event."""

    def __init__(self, config, catalog, requester=None, interval=10, timeout=1.5,
                 connect=None):
        if not isinstance(config, dict):
            raise ValueError("Invalid paired Hue configuration")
        bridge, key = _local_ip(config.get("bridge")), config.get("hue_key")
        if (bridge is None or not isinstance(key, str) or not 1 <= len(key) <= 256
                or any(not 33 <= ord(char) <= 126 for char in key)):
            raise ValueError("Invalid paired Hue configuration")
        if (not _finite_positive(interval) or not _finite_positive(timeout)
                or interval > 3600 or timeout > 5):
            raise ValueError("Invalid Hue discovery bounds")
        self._bridge, self._key = bridge, key
        self._catalog, self._interval, self._timeout = catalog, interval, timeout
        # connect(timeout) returns a connection that verified the pinned bridge
        # (the adapter's HueBridgeLink); the key is never sent without it.
        self._connect = connect
        self._requester = requester or self._request

    def _request(self, path):
        if path not in _PATHS:
            raise ValueError("Unexpected Hue discovery resource")
        if self._connect is None:
            raise OSError("No verified Hue bridge connection")
        deadline = time.monotonic() + self._timeout
        connection = self._connect(self._timeout)
        try:
            connection.request("GET", path, headers={"hue-application-key": self._key,
                                                     "Accept": "application/json"})
            response = connection.getresponse()
            if response.status != 200:
                raise OSError("Hue discovery request failed")
            raw = bytearray()
            while len(raw) <= _MAX_BYTES:
                if time.monotonic() >= deadline:
                    raise ValueError("Hue discovery response timed out")
                chunk = response.read1(min(65536, _MAX_BYTES + 1 - len(raw)))
                if not chunk:
                    break
                raw.extend(chunk)
            if len(raw) > _MAX_BYTES:
                raise ValueError("Hue discovery response too large")
        except http.client.HTTPException:
            raise ValueError("Invalid Hue discovery response") from None
        finally:
            connection.close()
        return json.loads(raw)

    def _poll(self, stop_event=None):
        try:
            resources = []
            for path in _PATHS:
                if stop_event is not None and stop_event.is_set():
                    return False
                response = self._requester(path)
                if (not isinstance(response, dict) or response.get("errors") != []
                        or not isinstance(response.get("data"), list)):
                    raise ValueError("Invalid Hue discovery response")
                resources.append(response["data"])
            if stop_event is not None and stop_event.is_set():
                return False
            bridges, devices, lights = resources
            if len(bridges) != 1 or not isinstance(bridges[0], dict):
                raise ValueError("Invalid Hue bridge identity")
            bridge_id = bridges[0].get("bridge_id")
            if not isinstance(bridge_id, str) or not re.fullmatch(r"[0-9a-fA-F]{16}", bridge_id):
                raise ValueError("Invalid Hue bridge identity")
            self._catalog.observe_hue(bridge_id, lights, devices)
            self._catalog.provider_state("hue", "ready")
            return True
        except Exception:
            # Errors can include credentials/addresses; publish only a fixed state.
            self._catalog.provider_state("hue", "unavailable")
            return False

    def poll_once(self):
        return self._poll()

    def run(self, stop_event):
        while not stop_event.is_set():
            self._poll(stop_event)
            if stop_event.wait(self._interval):
                return
