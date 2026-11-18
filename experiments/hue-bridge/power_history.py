"""Bounded history of Govee power-gate changes for Sync diagnostics.

A power authority records the gate it publishes for a role after each change:
whether the owner wants the lamp on, whether the color writer may send and
whether a Sync hold switched the lamp off. Timestamps use the wall clock in the
same format as the stream journal, so power changes line up with Sync events.
"""

import threading
import time
from collections import deque
from datetime import datetime, timezone

MAX_EVENTS = 64
CAUSES = frozenset({
    "owner",            # owner heartbeat changed intent or renewed an expired grant
    "grant_expired",    # owner heartbeat stopped; color permission lapsed
    "intent",           # HomeKit or Hue App power request
    "confirmed",        # native reading confirmed On
    "adopted",          # native change made outside HomeKit became intent
    "sync_hold",        # lamp switched off for a Sync session
    "sync_release",     # Sync hold ended
    "sync_restore_failed",  # On after Sync was not confirmed; the hold stays
    "owner_power",      # explicit owner power request ended a Sync hold
})


def timestamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")


class PowerHistory:
    def __init__(self, clock=time.monotonic, wall_clock=time.time, limit=MAX_EVENTS):
        self.clock, self.wall_clock = clock, wall_clock
        self.lock = threading.Lock()
        self.events = deque(maxlen=limit)
        self.last = {}

    def gate(self, role):
        """The last recorded (requested_on, color_allowed, sync_hold) or None."""
        with self.lock:
            return self.last.get(role)

    def record(self, role, cause, requested_on, color_allowed, sync_hold, at=None):
        """Record a changed gate; ``at`` is the monotonic time of an earlier change."""
        if cause not in CAUSES:
            raise ValueError("Unknown power change cause")
        gate = (requested_on, color_allowed, sync_hold)
        with self.lock:
            if self.last.get(role) == gate:
                return
            self.last[role] = gate
            now = self.clock()
            when = now if at is None else min(at, now)
            try:
                stamp = timestamp(self.wall_clock() - (now - when))
            except (OverflowError, OSError, ValueError):
                stamp = None
            self.events.append({
                "timestamp": stamp, "role": role, "cause": cause,
                "requested_on": requested_on, "color_allowed": color_allowed,
                "sync_hold": sync_hold,
            })

    def snapshot(self):
        with self.lock:
            return [dict(event) for event in self.events]
