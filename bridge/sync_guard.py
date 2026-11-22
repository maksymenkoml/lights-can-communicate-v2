"""Restart Sync on the Sync Box when a bridge restart or a stall cost it its frames.

A restart of the bridge (an update, a crash, Docker) while the box streams makes
the box stop Sync, or leaves it reporting Sync on while no frame arrives; while
the bridge warms up the box also drops a new Start. Only a new Start from the
box recovers it. The guard does that, a few times with growing pauses, and never
overrides a Stop: Sync stopped while the bridge runs undisturbed stays off.
"""

import threading
import time

# Frames normally arrive within about 11 s of a Start.
STALL_SECONDS = 20.0
# A box that drops Sync this soon after a Start gave up; it did not obey a Stop.
DROP_SECONDS = 30.0
# Extra wait before each restart, beyond STALL_SECONDS without frames since the
# last Start. A box whose stream was lost ignored Starts for 4-6 minutes in live
# checks (it came back at 232, 295 and 358 s); with the quiet times below the
# Stops fall at about 25, 85, 155, 285, 415 and 575 s after the stall began.
RETRY_DELAYS = (0.0, 30.0, 30.0, 60.0, 60.0, 90.0)
# Quiet time between Stop and Start, per attempt. A box that lost its stream came
# back in a live check only after about 50 s off; 6 s and 22 s did not.
RESTART_PAUSES = (3.0, 15.0, 45.0, 45.0, 45.0, 45.0)
# Sync that stops up to 60 s after a bridge restart was seen was stopped by it.
# The bridge must already be going down when the Stop is seen: the probe misses
# or the frame counter resets in the same check or the next one, never later,
# so a Stop while the bridge runs undisturbed stays.
RESTART_BEFORE = 60.0
RESTART_AFTER = 6.0
TICK_SECONDS = 5.0
# Stream states that mean no frames: the adapter answers and nobody else streams.
STALLED = ("waiting", "off")


class SyncGuard:
    """Watch the box and the stream; restart Sync when frames do not come."""

    def __init__(self, box, stream_state, bridge_frames=None,
                 clock=time.monotonic, sleep=time.sleep):
        self.box = box
        self.stream_state = stream_state
        # Frames the bridge received since it started, or None when it does not answer.
        self.bridge_frames = bridge_frames
        self.frames = None
        self.bridge_restarted_at = None
        self.was_syncing = False
        self.lost_at = None
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
        # Bumped by the owner's Start and Stop; a restart that was pausing gives up.
        self.generation = 0

    # The owner's own Start and Stop from Light Studio.
    def started(self):
        with self.lock:
            self.generation += 1
            self.started_at = self.clock()
            self.stalled_since = None
            self.attempts = 0
            self.retrying = False
            self.state = None

    def stopped(self):
        with self.lock:
            self.generation += 1
            self.was_syncing = False
            self.lost_at = None
            self.started_at = None
            self.stalled_since = None
            self.attempts = 0
            self.retrying = False
            self.state = None

    def public(self):
        """The recovery state for the page: {"recovery": "restarting" | "failed"} or {}."""
        with self.lock:
            return {"recovery": self.state} if self.state else {}

    def _watch_bridge(self, now):
        if self.bridge_frames is None:
            return
        try:
            frames = self.bridge_frames()
        except Exception:  # noqa: BLE001 - an unreadable bridge is a bridge that is down
            frames = None
        if frames is None or (self.frames is not None and frames < self.frames):
            self.bridge_restarted_at = now
        if frames is not None:
            self.frames = frames

    def tick(self):
        """One check; returns True when it restarted Sync."""
        now = self.clock()
        self._watch_bridge(now)
        status = self.box.status()
        if status.get("state") != "ready" or status.get("changing") is not None:
            return False
        syncing = bool(status.get("syncing"))
        with self.lock:
            if self.was_syncing and not syncing:
                self.lost_at = now
            self.was_syncing = syncing
            if self.lost_at is not None and now - self.lost_at > RESTART_AFTER:
                self.lost_at = None
            if (not syncing and self.lost_at is not None and not self.retrying
                    and self.bridge_restarted_at is not None
                    and -RESTART_BEFORE <= self.bridge_restarted_at - self.lost_at
                    <= RESTART_AFTER):
                # The bridge restart stopped Sync: start it again, as if it dropped.
                self.lost_at = None
                self.retrying = True
                self.started_at = now
                self.stalled_since = now - STALL_SECONDS
                self.attempts = 0
        if syncing:
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
        with self.lock:
            generation = self.generation
        self.box.set_sync(False)
        with self.lock:
            pause = RESTART_PAUSES[min(max(self.attempts, 1), len(RESTART_PAUSES)) - 1]
        self.sleep(pause)
        with self.lock:
            if self.generation != generation:
                # The owner started or stopped Sync meanwhile; theirs is the last word.
                return False
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
