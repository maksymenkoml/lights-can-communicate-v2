"""Translate successful explicit Hue API power writes into confirmed owner requests."""

import json
import time
import urllib.error
import urllib.request
import uuid


class HuePowerUnavailable(RuntimeError):
    """A safe API diagnostic that contains no owner credentials or identifiers."""


class HuePowerRequests:
    SLOTS = {"1": "left", "2": "right"}

    def __init__(self, url, token, lights, groups, feedback, sender=None, enabled=lambda: True):
        self.url, self.token = url.rstrip("/"), token
        self.lights, self.groups, self.feedback = lights, groups, feedback
        self.sender, self.enabled = sender or self.send, enabled

    def send(self, states):
        request = urllib.request.Request(
            self.url + "/power-request", method="POST",
            data=json.dumps({"id": uuid.uuid4().hex, "issued_at": time.time(),
                             "states": states}).encode(),
            headers={"Content-Type": "application/json", "X-Adapter-Token": self.token},
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=13) as response:
            encoded = response.read(4097)
            if len(encoded) > 4096:
                raise ValueError("Invalid power response")
            result = json.loads(encoded)
            if type(result) is not dict or result.get("accepted") is not True:
                raise ValueError("Power was not confirmed")

    def snapshot(self):
        return {slot: lamp.state["on"] for slot, lamp in self.lights.items()
                if slot in self.SLOTS and type(lamp.state.get("on")) is bool}

    def _selected(self, resource, rid, v2):
        if resource in {"light", "lights"}:
            return [lamp for slot, lamp in self.lights.items()
                    if slot in self.SLOTS and lamp.id_v1 == slot
                    and (lamp.id_v2 if v2 else slot) == rid]
        for group in self.groups.values():
            if group.type == "Entertainment":
                continue
            identity = group.getV2GroupedLight()["id"] if v2 else group.id_v1
            if identity == rid:
                return [lamp for reference in group.lights if (lamp := reference()) is not None
                        and lamp.id_v1 in self.SLOTS
                        and self.lights.get(lamp.id_v1) is lamp]
        return []

    def after_request(self, method, path, status, payload, response, previous=None):
        if method != "PUT" or not 200 <= status < 300 or type(payload) is not dict:
            return
        parts = path.strip("/").split("/")
        v2 = len(parts) == 5 and parts[:3] == ["clip", "v2", "resource"]
        v1 = (len(parts) == 5 and parts[0] == "api"
              and (parts[2], parts[4]) in {("lights", "state"), ("groups", "action")})
        if v2:
            resource, rid = parts[3:]
            if resource not in {"light", "grouped_light"} or "on" not in payload:
                return
            if (type(response) is not dict or response.get("errors")
                    or response.get("data") != [{"rid": rid, "rtype": resource}]):
                return
            on = payload["on"]
            if type(on) is not dict or set(on) != {"on"}:
                raise HuePowerUnavailable("Invalid explicit power request")
            value = on["on"]
        elif v1:
            resource, rid = parts[2:4]
            if "scene" in payload or "on" not in payload:
                return
            address = f"/{resource}/{rid}/{parts[4]}/on"
            if (type(response) is not list or not response
                    or any(type(item) is not dict or "error" in item for item in response)
                    or not any(item.get("success") == {address: payload["on"]}
                               for item in response)):
                return
            value = payload["on"]
        else:
            return
        selected = self._selected(resource, rid, v2)
        if not selected:
            return
        if type(value) is not bool:
            raise HuePowerUnavailable("Invalid explicit power request")
        confirmed = False
        try:
            if not self.enabled():
                raise HuePowerUnavailable("Explicit power control is unavailable")
            self.sender({self.SLOTS[lamp.id_v1]: value for lamp in selected})
            confirmed = True
        except (OSError, ValueError, TypeError) as error:
            if isinstance(error, urllib.error.HTTPError):
                error.close()
            raise HuePowerUnavailable("Power request was not confirmed") from None
        finally:
            slots = {lamp.id_v1 for lamp in selected}
            fallback = ({slot: before for slot, before in (previous or {}).items() if slot in slots}
                        if not confirmed else None)
            self.feedback.refresh_once(force=slots, fallback=fallback)
