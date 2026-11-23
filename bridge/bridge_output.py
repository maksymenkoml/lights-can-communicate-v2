"""Coalescing authenticated bridge-to-host transition delivery for the live adapter."""

import copy
import json
import threading
import time
import urllib.request
import uuid
from collections.abc import MutableMapping

STRIP_COLOR_FIELDS = {"gradient", "xy", "ct", "hue", "sat"}


def desk_palette_index(light_id, palette, fallback):
    colors = palette.get("color", [])
    offsets = {"1": 0, "2": 4, "3": 0, "4": 0, "5": 2, "6": 4}
    return offsets[str(light_id)] % len(colors) if colors and str(light_id) in offsets else fallback


def strip_changes(state):
    changed = state.get("_changed")
    if changed is None:
        changed = [key for key in ("bri", "xy", "ct", "gradient", "hue", "sat") if key in state]
    if not isinstance(changed, list) or not all(isinstance(key, str) for key in changed):
        raise ValueError("Invalid changed fields")
    changed = set(changed)
    if state.get("on") is True and "on" in changed and not (changed & STRIP_COLOR_FIELDS):
        if "bri" in state:
            changed.add("bri")
        kind = state.get("_color_kind")
        if kind is None:
            kind = (
                "ct"
                if state.get("colormode") == "ct"
                else "gradient"
                if state.get("gradient", {}).get("points")
                else "xy"
            )
        if kind not in {"ct", "gradient", "xy", "hs"}:
            raise ValueError("Invalid strip color kind")
        changed.update({"hue", "sat"} if kind == "hs" else {kind})
    return changed


class _WorkerDynamics(MutableMapping):
    def __init__(self, guard, lamp, generation):
        self.guard, self.lamp, self.generation = guard, lamp, generation

    def __getitem__(self, key):
        with self.guard.lock:
            if key == "status" and not self.guard.current(self.lamp, self.generation):
                return "none"
            return self.lamp.dynamics[key]

    def __setitem__(self, key, value):
        with self.guard.lock:
            if self.guard.current(self.lamp, self.generation):
                self.lamp.dynamics[key] = value

    def __delitem__(self, key):
        with self.guard.lock:
            if self.guard.current(self.lamp, self.generation):
                del self.lamp.dynamics[key]

    def __iter__(self):
        return iter(self.lamp.dynamics)

    def __len__(self):
        return len(self.lamp.dynamics)


class _WorkerLight:
    def __init__(self, guard, lamp, generation):
        self.guard, self.lamp, self.generation = guard, lamp, generation
        self.dynamics = _WorkerDynamics(guard, lamp, generation)

    def __getattr__(self, name):
        return getattr(self.lamp, name)

    def setV2State(self, state):
        with self.guard.lock:
            if self.guard.current(self.lamp, self.generation):
                self.lamp.setV2State(state)


class PlayGuard:
    def __init__(self):
        self.lock = threading.RLock()
        self.generations = {}
        self.local = threading.local()

    def current(self, lamp, generation):
        return self.generations.get(lamp.id_v1) == generation

    def worker_generation(self, lamp):
        context = getattr(self.local, "context", None)
        return context[1] if context and context[0] == lamp.id_v1 else None

    def cancel(self, lamp):
        with self.lock:
            self.generations[lamp.id_v1] = self.generations.get(lamp.id_v1, 0) + 1
            lamp.dynamics["status"] = "none"

    def prepare(self, lamp, palette, index, original):
        with self.lock:
            generation = self.generations.get(lamp.id_v1, 0) + 1
            self.generations[lamp.id_v1] = generation
        palette = copy.deepcopy(palette)

        def worker():
            with self.lock:
                if not self.current(lamp, generation):
                    return
            previous = getattr(self.local, "context", None)
            self.local.context = (lamp.id_v1, generation)
            try:
                return original(_WorkerLight(self, lamp, generation), palette, index)
            finally:
                self.local.context = previous

        return worker

    def run(self, lamp, palette, index, original):
        return self.prepare(lamp, palette, index, original)()

    def thread_factory(self, constructor, dynamic_method, index_selector=None):
        def create(*args, **kwargs):
            target = kwargs.get("target")
            if getattr(target, "__func__", None) is dynamic_method:
                lamp = target.__self__
                palette, index = kwargs["args"]
                if index_selector is not None:
                    index = index_selector(lamp.id_v1, palette, index)
                kwargs = {
                    **kwargs,
                    "target": self.prepare(lamp, palette, index, dynamic_method),
                    "args": (),
                }
            return constructor(*args, **kwargs)

        return create


class BridgeOutput:
    def __init__(
        self, url, token, sender=None, clock=time.time, include_strip=False, include_hue_play=False,
        govee_slots=None,
    ):
        self.url, self.token, self.clock = url.rstrip("/"), token, clock
        self.sender = sender or self.post
        self.session = str(uuid.uuid4())
        self.condition = threading.Condition()
        self.pending, self.sequences, self.latest = {}, {}, {}
        self.inflight = {}
        self.forwarded = 0
        self.last_error = None
        # Virtual slot -> Govee light id from the registry; the migrated lamps by default.
        govee = dict(govee_slots) if govee_slots is not None else {
            "1": "govee-left", "2": "govee-right"}
        govee_lights = set(govee.values())
        self.slots = govee
        if include_strip:
            self.slots["3"] = "hue-monitor"
        if include_hue_play:
            self.slots.update({"4": "play-left", "5": "play-center", "6": "play-right"})
        self.hue_roles = set(self.slots.values()) - govee_lights

    def submit(self, light_id, state, transitiontime):
        light = self.slots.get(str(light_id))
        if light is None:
            return False
        with self.condition:
            state = copy.deepcopy(state)
            if light in self.hue_roles and state.get("on") is True:
                previous = self.pending.get(light) or self.inflight.get(light)
                changed = strip_changes(state)
                if previous and previous["state"].get("on") is True:
                    old = strip_changes(previous["state"])
                    if changed & STRIP_COLOR_FIELDS:
                        old -= STRIP_COLOR_FIELDS
                    changed |= old
                state["_changed"] = sorted(changed)
            sequence = self.sequences.get(light, 0) + 1
            self.sequences[light] = sequence
            payload = {
                "light": light,
                "session": self.session,
                "sequence": sequence,
                "issued_at": self.clock(),
                "transitiontime": transitiontime,
                "state": copy.deepcopy(state),
            }
            self.pending[light] = payload
            self.latest[light] = {
                "sequence": sequence,
                "transitiontime": transitiontime,
                "issued_at": payload["issued_at"],
            }
            self.condition.notify()
        return True

    def post(self, payload):
        request = urllib.request.Request(
            self.url + "/command",
            data=json.dumps(payload).encode(),
            method="POST",
            headers={"Content-Type": "application/json", "X-Adapter-Token": self.token},
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=1) as response:
            json.load(response)

    def forward_once(self):
        with self.condition:
            if not self.pending:
                return False
            light = next(iter(self.pending))
            payload = self.pending.pop(light)
            self.inflight[light] = payload
        if self.clock() - payload["issued_at"] > 5:
            self.last_error = "expired command"
            with self.condition:
                self.inflight.pop(light, None)
            return False
        try:
            self.sender(payload)
        except (OSError, ValueError) as error:
            with self.condition:
                if light not in self.pending:
                    self.pending[light] = payload
                self.inflight.pop(light, None)
                self.last_error = type(error).__name__
            return False
        with self.condition:
            self.inflight.pop(light, None)
            self.forwarded += 1
            self.last_error = None
        return True

    def start(self):
        def worker():
            while True:
                with self.condition:
                    if not self.pending:
                        self.condition.wait(timeout=1)
                if not self.forward_once():
                    time.sleep(0.2)

        threading.Thread(target=worker, daemon=True, name="probe-live-output").start()

    def snapshot(self):
        with self.condition:
            return {
                "enabled": True,
                "forwarded": self.forwarded,
                "pending": sorted(self.pending),
                "last_error": self.last_error,
                "latest": copy.deepcopy(self.latest),
            }
