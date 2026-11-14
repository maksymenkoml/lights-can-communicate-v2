"""Project fresh NAS power intent into the two selected virtual Hue lights."""

import json
import math
import threading
import time
import urllib.request


class PowerFeedback:
    SLOTS = {"1": "left", "2": "right"}

    def __init__(self, url, token, lights, lock, resume, publish_groups, fetcher=None):
        self.url, self.token = url.rstrip("/"), token
        self.lights, self.lock = lights, lock
        self.resume, self.publish_groups = resume, publish_groups
        self.fetcher = fetcher or self.fetch
        self.states, self.revisions = {}, {}
        self.last_error = None

    def fetch(self):
        request = urllib.request.Request(
            self.url + "/status", headers={"X-Adapter-Token": self.token}
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=0.75) as response:
            return json.load(response)

    def refresh_once(self):
        try:
            payload = self.fetcher()
            authority = payload.get("power_owner") if isinstance(payload, dict) else None
            if not isinstance(authority, dict) or authority.get("enforced") is not True:
                raise ValueError("Power authority unavailable")
            roles = authority.get("roles")
            if not isinstance(roles, dict) or set(roles) != {"left", "right"}:
                raise ValueError("Invalid power roles")
            updates = {}
            for slot, side in self.SLOTS.items():
                value = roles[side]
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
                    # An unchanged heartbeat must not override a Hue-local Off.
                    # Off remains authoritative even if a scene prewrites state.
                    if (new_intent or not enabled) and lamp.state.get("on") is not enabled:
                        lamp.state["on"] = enabled
                        lamp.genStreamEvent({"on": {"on": enabled}})
                        changed.add(slot)
                    if enabled and new_intent:
                        self.resume(lamp)
                if changed:
                    self.publish_groups(changed)
                self.last_error = None
            return True
        except (OSError, ValueError, TypeError, KeyError) as error:
            with self.lock:
                self.last_error = type(error).__name__
            return False

    def constrain(self, lamp, values):
        with self.lock:
            if self.states.get(lamp.id_v1) is False:
                return {**values, "on": False}
            return values

    def snapshot(self):
        with self.lock:
            return {"enabled": True, "states": dict(self.states), "last_error": self.last_error}

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
