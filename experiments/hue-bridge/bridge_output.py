"""Coalescing authenticated bridge-to-host transition delivery for the pilot."""

import copy
import json
import threading
import time
import urllib.request
import uuid
from collections.abc import MutableMapping


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

    def thread_factory(self, constructor, dynamic_method):
        def create(*args, **kwargs):
            target = kwargs.get("target")
            if getattr(target, "__func__", None) is dynamic_method:
                lamp = target.__self__
                palette, index = kwargs["args"]
                kwargs = {
                    **kwargs,
                    "target": self.prepare(lamp, palette, index, dynamic_method),
                    "args": (),
                }
            return constructor(*args, **kwargs)

        return create


class BridgeOutput:
    def __init__(self, url, token, sender=None, clock=time.time):
        self.url, self.token, self.clock = url.rstrip("/"), token, clock
        self.sender = sender or self.post
        self.session = str(uuid.uuid4())
        self.condition = threading.Condition()
        self.pending, self.sequences, self.latest = {}, {}, {}
        self.forwarded = 0
        self.last_error = None

    def submit(self, light_id, state, transitiontime):
        side = {"1": "left", "2": "right"}.get(str(light_id))
        if side is None:
            return False
        with self.condition:
            sequence = self.sequences.get(side, 0) + 1
            self.sequences[side] = sequence
            payload = {
                "side": side,
                "session": self.session,
                "sequence": sequence,
                "issued_at": self.clock(),
                "transitiontime": transitiontime,
                "state": copy.deepcopy(state),
            }
            self.pending[side] = payload
            self.latest[side] = {
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
            side = next(iter(self.pending))
            payload = self.pending.pop(side)
        if self.clock() - payload["issued_at"] > 5:
            self.last_error = "expired command"
            return False
        try:
            self.sender(payload)
        except (OSError, ValueError) as error:
            with self.condition:
                if side not in self.pending:
                    self.pending[side] = payload
                self.last_error = type(error).__name__
            return False
        with self.condition:
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
