"""Project fresh NAS power intent into the selected virtual Hue lights."""

import json
import math
import threading
import time
import urllib.request


class PowerFeedback:
    # The Govee light behind each virtual slot; the bridge passes the registry map.
    SLOTS = {"1": "govee-left", "2": "govee-right"}
    # Native Hue lights mirror the genuine bridge as the adapter last read it.
    NATIVE_SLOTS = {"3": "strip", "4": "hue_left", "5": "hue_center", "6": "hue_right"}
    NATIVE_FRESH = 5.0
    NATIVE_HOLD = 15.0

    def __init__(self, url, token, lights, lock, resume, publish_groups, fetcher=None,
                 clock=time.monotonic, native_slots=None, slots=None):
        if native_slots is not None:
            self.NATIVE_SLOTS = dict(native_slots)
        if slots is not None:
            self.SLOTS = dict(slots)
        self.url, self.token = url.rstrip("/"), token
        self.lights, self.lock = lights, lock
        self.resume, self.publish_groups = resume, publish_groups
        self.fetcher = fetcher or self.fetch
        self.states, self.revisions = {}, {}
        self.clock, self.corrections = clock, {}
        self.last_error = None
        self.native_holds = {}

    def fetch(self):
        request = urllib.request.Request(
            self.url + "/status", headers={"X-Adapter-Token": self.token}
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=0.75) as response:
            return json.load(response)

    def restore_known(self, previous):
        """Undo a rejected virtual write without claiming a fresh physical reading."""
        changed = set()
        for slot, value in previous.items():
            if slot not in self.SLOTS or type(value) is not bool:
                continue
            enabled = self.states.get(slot, value)
            lamp = self.lights[slot]
            if lamp.state.get("on") is not enabled:
                lamp.state["on"] = enabled
                lamp.genStreamEvent({"on": {"on": enabled}})
                changed.add(slot)
                if enabled:
                    self.resume(lamp)
        if changed:
            self.publish_groups(changed)

    def hold_native(self, slots):
        with self.lock:
            for slot in slots:
                self.native_holds[slot] = self.clock() + self.NATIVE_HOLD

    def release_native(self, slots):
        with self.lock:
            for slot in slots:
                self.native_holds.pop(slot, None)

    def restore_native(self, previous):
        """Undo a rejected virtual write of native lights."""
        with self.lock:
            changed = set()
            for slot, value in previous.items():
                lamp = self.lights.get(slot)
                if (slot in self.NATIVE_SLOTS and lamp is not None and type(value) is bool
                        and lamp.state.get("on") is not value):
                    lamp.state["on"] = value
                    lamp.genStreamEvent({"on": {"on": value}})
                    changed.add(slot)
            if changed:
                self.publish_groups(changed)

    def mirror_native(self, payload):
        """Show the actual native power of fresh, healthy proxies in the virtual lights."""
        # The adapter reports the strip separately and every other native role
        # under hue_play; any configured role is mirrored the same way.
        proxies = dict(payload.get("hue_play") or {}) if isinstance(payload, dict) else {}
        if isinstance(payload, dict) and isinstance(payload.get("hue_strip"), dict):
            proxies["strip"] = payload["hue_strip"]
        with self.lock:
            changed = set()
            now = self.clock()
            for slot, role in self.NATIVE_SLOTS.items():
                lamp, value = self.lights.get(slot), proxies.get(role)
                if lamp is None or not isinstance(value, dict):
                    continue
                if self.native_holds.get(slot, float("-inf")) > now:
                    continue
                on, age = value.get("native_on"), value.get("native_age")
                if (type(on) is not bool or type(age) not in (int, float)
                        or not math.isfinite(age) or not 0 <= age <= self.NATIVE_FRESH):
                    continue
                if lamp.state.get("on") is not on:
                    lamp.state["on"] = on
                    lamp.genStreamEvent({"on": {"on": on}})
                    changed.add(slot)
            if changed:
                self.publish_groups(changed)

    def refresh_once(self, force=(), fallback=None):
        try:
            payload = self.fetcher()
            self.mirror_native(payload)
            authority = payload.get("power_owner") if isinstance(payload, dict) else None
            if not isinstance(authority, dict) or authority.get("enforced") is not True:
                raise ValueError("Power authority unavailable")
            lights = authority.get("lights")
            # A slot follows its light only when the registry enrolls it; each
            # enrolled slot is validated and updated on its own.
            enrolled = ({slot: light for slot, light in self.SLOTS.items() if light in lights}
                        if isinstance(lights, dict) else {})
            if not enrolled:
                raise ValueError("Invalid power lights")
            updates = {}
            for slot, light in enrolled.items():
                value = lights[light]
                if not isinstance(value, dict):
                    raise ValueError("Invalid role state")
                enabled, revision, remaining = (
                    value.get(key) for key in ("requested_on", "revision", "grant_remaining")
                )
                if (type(enabled) is not bool or type(revision) is not int or revision < 0
                        or type(remaining) not in (int, float) or not math.isfinite(remaining)
                        or not 0 <= remaining <= 2):
                    raise ValueError("Invalid role state")
                if remaining > 0:
                    updates[slot] = (enabled, revision)
            with self.lock:
                changed = set()
                for slot, (enabled, revision) in updates.items():
                    lamp = self.lights[slot]
                    new_intent = (enabled, revision) != (
                        self.states.get(slot), self.revisions.get(slot)
                    )
                    self.states[slot], self.revisions[slot] = enabled, revision
                    due = self.corrections.get(slot, float("inf")) <= self.clock()
                    if due or enabled:
                        self.corrections.pop(slot, None)
                    # An unchanged heartbeat must not override a Hue-local Off.
                    # Off remains authoritative even if a scene prewrites state.
                    if ((new_intent or not enabled or slot in force)
                            and lamp.state.get("on") is not enabled
                            or (due and not enabled)):
                        lamp.state["on"] = enabled
                        lamp.genStreamEvent({"on": {"on": enabled}})
                        changed.add(slot)
                    if enabled and (new_intent or (slot in force and slot in changed)):
                        self.resume(lamp)
                if changed:
                    self.publish_groups(changed)
                missing = set(force) - updates.keys()
                if fallback and missing:
                    self.restore_known({slot: value for slot, value in fallback.items()
                                        if slot in missing})
                unavailable = set(enrolled) - updates.keys()
                self.last_error = "PowerAuthorityUnavailable" if unavailable else None
            return not unavailable
        except (OSError, ValueError, TypeError, KeyError) as error:
            with self.lock:
                if fallback:
                    self.restore_known(fallback)
                self.last_error = type(error).__name__
            return False

    def after_request(self, method, path, status, payload):
        if (method != "PUT" or not 200 <= status < 300 or not isinstance(payload, dict)
                or not path.startswith(("/clip/v2/resource/scene/", "/clip/v2/resource/light/",
                                        "/clip/v2/resource/grouped_light/"))):
            return
        on = payload.get("on")
        if "recall" not in payload and not (isinstance(on, dict) and on.get("on") is True):
            return
        with self.lock:
            for slot, enabled in self.states.items():
                if not enabled:
                    # Hue applies a scene's stored On locally after its response,
                    # potentially overwriting the immediate SSE Off. Reassert
                    # once after that reply, without a physical power command.
                    self.corrections[slot] = self.clock() + 1

    def constrain(self, lamp, values):
        with self.lock:
            if self.states.get(lamp.id_v1) is False:
                return {**values, "on": False}
            return values

    def snapshot(self):
        with self.lock:
            return {"enabled": True, "states": dict(self.states), "last_error": self.last_error,
                    "pending_corrections": sorted(self.corrections)}

    def constrain_v2(self, lamp, values):
        with self.lock:
            if self.states.get(lamp.id_v1) is False and "on" in values:
                return {**values, "on": {"on": False}}
            return values

    def start(self, enabled=lambda: True):
        def worker():
            while True:
                if enabled():
                    self.refresh_once()
                time.sleep(0.25)

        threading.Thread(target=worker, daemon=True, name="hue-power-feedback").start()
