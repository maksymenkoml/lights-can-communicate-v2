"""Translate successful explicit Hue API power writes into confirmed owner requests.

A scene recall switches each light of the scene On or Off as the scene stores it,
as on a Hue bridge. Without that, a light stored Off in a scene kept its old colour,
and the owner could not see why the scene did not reach it.
"""

import json
import time
import urllib.error
import urllib.request
import uuid


class HuePowerUnavailable(RuntimeError):
    """A safe API diagnostic that contains no owner credentials or identifiers."""


class HuePowerRequests:
    # The Govee light behind each virtual slot; the bridge passes the registry map.
    SLOTS = {"1": "govee-left", "2": "govee-right"}
    # Pinned native Hue lights; their power belongs to the genuine Hue bridge.
    # The bridge passes the same slot map it uses for color, so every native
    # light enrolled for output also gets power; this is the default layout.
    NATIVE_SLOTS = {"3": "hue-monitor", "4": "play-left", "5": "play-center", "6": "play-right"}

    def __init__(self, url, token, lights, groups, feedback, sender=None, enabled=lambda: True,
                 native_sender=None, native_slots=None, slots=None, scenes=None):
        if native_slots is not None:
            self.NATIVE_SLOTS = dict(native_slots)
        if slots is not None:
            self.SLOTS = dict(slots)
        self.url, self.token = url.rstrip("/"), token
        self.lights, self.groups, self.feedback = lights, groups, feedback
        # The bridge's scenes, by v1 id (diyHue's config["scenes"]).
        self.scenes = scenes if scenes is not None else {}
        self.sender, self.enabled = sender or self.send, enabled
        self.native_sender = native_sender or self.send_native

    def _post(self, path, body):
        request = urllib.request.Request(
            self.url + path, method="POST", data=json.dumps(body).encode(),
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

    def send_native(self, lights):
        self._post("/native-power", {"lights": lights})

    def send(self, states):
        self._post("/power-request", {"id": uuid.uuid4().hex, "issued_at": time.time(),
                                      "states": states})

    def snapshot(self):
        return {slot: lamp.state["on"] for slot, lamp in self.lights.items()
                if (slot in self.SLOTS or slot in self.NATIVE_SLOTS)
                and type(lamp.state.get("on")) is bool}

    def _selected(self, resource, rid, v2, slots=None):
        slots = self.SLOTS if slots is None else slots
        if resource in {"light", "lights"}:
            return [lamp for slot, lamp in self.lights.items()
                    if slot in slots and lamp.id_v1 == slot
                    and (lamp.id_v2 if v2 else slot) == rid]
        for group in self.groups.values():
            if group.type == "Entertainment":
                continue
            identity = group.getV2GroupedLight()["id"] if v2 else group.id_v1
            if identity == rid:
                return [lamp for reference in group.lights if (lamp := reference()) is not None
                        and lamp.id_v1 in slots
                        and self.lights.get(lamp.id_v1) is lamp]
        return []

    def _recalled(self, parts, payload, response):
        """The scene a successful recall request names, or None."""
        if parts[:4] == ["clip", "v2", "resource", "scene"] and len(parts) == 5:
            if (type(payload.get("recall")) is not dict or type(response) is not dict
                    or response.get("errors")
                    or response.get("data") != [{"rid": parts[4], "rtype": "scene"}]):
                return None
            return next((scene for scene in list(self.scenes.values())
                         if getattr(scene, "id_v2", None) == parts[4]), None)
        if (len(parts) == 5 and parts[0] == "api" and parts[2] == "groups"
                and parts[4] == "action" and isinstance(payload.get("scene"), str)):
            if (type(response) is not list or not response
                    or any(type(item) is not dict or "error" in item for item in response)):
                return None
            return self.scenes.get(payload["scene"])
        return None

    def _recall(self, scene, previous):
        """Switch the scene's lights to the power it stores; only those that differ."""
        wanted = {}
        for lamp, state in list(scene.lightstates.items()):
            slot = getattr(lamp, "id_v1", None)
            if (type(state) is dict and type(state.get("on")) is bool
                    and self.lights.get(slot) is lamp
                    and (slot in self.SLOTS or slot in self.NATIVE_SLOTS)
                    and (previous or {}).get(slot) is not state["on"]):
                wanted[slot] = state["on"]
        for value in (False, True):
            lamps = [self.lights[slot] for slot, on in sorted(wanted.items()) if on is value]
            self._switch([lamp for lamp in lamps if lamp.id_v1 in self.NATIVE_SLOTS],
                         [lamp for lamp in lamps if lamp.id_v1 in self.SLOTS], value, previous)

    def after_request(self, method, path, status, payload, response, previous=None):
        if method != "PUT" or not 200 <= status < 300 or type(payload) is not dict:
            return
        parts = path.strip("/").split("/")
        scene = self._recalled(parts, payload, response)
        if scene is not None:
            self._recall(scene, previous)
            return
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
        native = self._selected(resource, rid, v2, self.NATIVE_SLOTS)
        selected = self._selected(resource, rid, v2)
        if not selected and not native:
            return
        if type(value) is not bool:
            raise HuePowerUnavailable("Invalid explicit power request")
        self._switch(native, selected, value, previous)

    def _switch(self, native, selected, value, previous):
        if native:
            self._native(native, value, previous)
        if not selected:
            return
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

    def _native(self, selected, value, previous):
        slots = {lamp.id_v1 for lamp in selected}
        # Keep feedback from mirroring the old state while the write is in flight.
        self.feedback.hold_native(slots)
        try:
            if not self.enabled():
                raise HuePowerUnavailable("Explicit power control is unavailable")
            self.native_sender({self.NATIVE_SLOTS[lamp.id_v1]: value for lamp in selected})
        except (HuePowerUnavailable, OSError, ValueError, TypeError) as error:
            if isinstance(error, urllib.error.HTTPError):
                error.close()
            self.feedback.release_native(slots)
            # Undo the rejected virtual write, then let fresh native readings
            # correct any light whose write took effect despite the failure.
            self.feedback.restore_native({slot: before for slot, before
                                          in (previous or {}).items() if slot in slots})
            self.feedback.refresh_once()
            raise HuePowerUnavailable("Power request was not confirmed") from None
        self.feedback.release_native(slots)
