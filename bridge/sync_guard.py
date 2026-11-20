"""Restart Sync on the Sync Box when it runs Sync but no frames reach the adapter.

A restart of the gateway (an update, a crash, Docker) while the box streams can
leave the box reporting Sync on while no frame arrives, or make the box drop
Sync right after a Start while the bridge is still warming up. Only a new Start
from the box recovers it. The guard does that, a few times with growing pauses,
and never overrides a Stop: Sync that was on and is stopped later is left off.
"""

import threading
import time

# Frames normally arrive within about 11 s of a Start.
STALL_SECONDS = 20.0
# A box that drops Sync this soon after a Start gave up; it did not obey a Stop.
DROP_SECONDS = 30.0
# Pauses before the first, second and third restart, counted from the stall.
RETRY_DELAYS = (0.0, 30.0, 90.0)
RESTART_PAUSE = 3.0
TICK_SECONDS = 5.0
# Stream states that mean no frames: the adapter answers and nobody else streams.
STALLED = ("waiting", "off")


class SyncGuard:
    """Watch the box and the stream; restart Sync when frames do not come."""

    def __init__(self, box, stream_state, clock=time.monotonic, sleep=time.sleep):
        self.box = box
        self.stream_state = stream_state
        self.clock = clock
        self.sleep = sleep
        self.lock = threading.Lock()
        # When Light Studio (the owner or this guard) last started Sync; a box that
        # drops Sync soon after it gave up. Sync started elsewhere is never restarted
        # after the box stops it.
        self.started_at = None
        self.stalled_since = None
        self.attempts = 0
        # True from a restart by the guard until frames flow: Sync is still wanted on.
        self.retrying = False
        self.state = None  # None, "restarting" or "failed"

    # The owner's own Start and Stop from Light Studio.
    def started(self):
        with self.lock:
            self.started_at = self.clock()
            self.stalled_since = None
            self.attempts = 0
            self.retrying = False
            self.state = None

    def stopped(self):
        with self.lock:
            self.started_at = None
            self.stalled_since = None
            self.attempts = 0
            self.retrying = False
            self.state = None

    def public(self):
        """The recovery state for the page: {"recovery": "restarting" | "failed"} or {}."""
        with self.lock:
            return {"recovery": self.state} if self.state else {}

    def tick(self):
        """One check; returns True when it restarted Sync."""
        status = self.box.status()
        if status.get("state") != "ready" or status.get("changing") is not None:
            return False
        now = self.clock()
        if status.get("syncing"):
            stream = self.stream_state()
            with self.lock:
                if stream == "active":
                    self.stalled_since = None
                    self.attempts = 0
                    self.retrying = False
                    self.state = None
                    return False
                if stream not in STALLED:
                    # The adapter is down or another app streams: not ours to fix.
                    self.stalled_since = None
                    return False
                if self.stalled_since is None:
                    self.stalled_since = now
                due = self._due(now)
            return self._restart() if due else False
        with self.lock:
            dropped = (self.started_at is not None and self.state != "failed"
                       and (self.retrying or now - self.started_at < DROP_SECONDS))
            if not dropped:
                if self.state != "failed":
                    self.state = None
                self.started_at = None
                self.stalled_since = None
                self.attempts = 0
                self.retrying = False
                return False
            if self.stalled_since is None:
                self.stalled_since = self.started_at
            due = self._due(now)
        return self._restart() if due else False

    def _due(self, now):
        if now - self.stalled_since < STALL_SECONDS:
            return False
        if self.attempts >= len(RETRY_DELAYS):
            self.state = "failed"
            return False
        if now - self.stalled_since < STALL_SECONDS + RETRY_DELAYS[self.attempts]:
            return False
        self.attempts += 1
        self.state = "restarting"
        return True

    def _restart(self):
        self.box.set_sync(False)
        self.sleep(RESTART_PAUSE)
        code, status = self.box.set_sync(True)
        with self.lock:
            # The next stall is counted from this Start.
            self.retrying = True
            self.started_at = self.clock()
            self.stalled_since = None
            if code != 200 or not status.get("syncing"):
                self.stalled_since = self.started_at - STALL_SECONDS
        return True

    def run(self, stop):
        """Check every few seconds until `stop` is set; errors never end the guard."""
        while not stop.wait(TICK_SECONDS):
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - one failed check must not stop the guard
                continue
