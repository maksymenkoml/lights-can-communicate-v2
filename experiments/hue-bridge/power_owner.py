"""Opt-in, short-lived owner permission for the two pinned Govee color writers."""

import math
import threading
import time
from collections import deque

import smart_lighting as lan

GRANT_SECONDS = 2.0
MAX_MESSAGE_AGE = 0.5


class PowerOwner:
    def __init__(self, targets, clock=time.monotonic, wall_clock=time.time):
        self.roles = {lan.device_key(device): device["side"] for device in targets}
        if len(self.roles) != 2 or set(self.roles.values()) != {"left", "right"}:
            raise ValueError("Power owner requires the two selected roles")
        self.clock, self.wall_clock = clock, wall_clock
        self.lock = threading.RLock()
        self.session = None
        self.sequence = -1
        self.issued = float("-inf")
        self.closed = deque(maxlen=64)
        self.states = {side: False for side in self.roles.values()}
        self.expires = {side: float("-inf") for side in self.states}
        self.revisions = {side: 0 for side in self.states}

    def revision(self, side):
        with self.lock:
            return self.revisions.get(side, 0)

    def allowed(self, side):
        with self.lock:
            return self.states.get(side) is True and self.clock() < self.expires.get(side, 0)

    def wrap(self, sender):
        def send(device, command, data):
            # The Off acknowledgement shares this lock with the complete UDP send.
            with self.lock:
                if not self.allowed(self.roles.get(lan.device_key(device))):
                    return False
                return sender(device, command, data)

        return send

    def submit(self, message):
        if not isinstance(message, dict) or set(message) != {
            "session", "sequence", "issued_at", "states"
        }:
            raise ValueError("Invalid power owner envelope")
        session, sequence, issued, states = (
            message[key] for key in ("session", "sequence", "issued_at", "states")
        )
        if (
            not isinstance(session, str) or not 1 <= len(session) <= 80
            or type(sequence) is not int or not 0 <= sequence < 2**63
            or type(issued) not in (float, int) or not math.isfinite(issued)
            or abs(self.wall_clock() - issued) > MAX_MESSAGE_AGE
            or not isinstance(states, dict) or not states or set(states) - set(self.states)
            or any(type(value) is not bool for value in states.values())
        ):
            raise ValueError("Invalid power owner state")
        with self.lock:
            if abs(self.wall_clock() - issued) > MAX_MESSAGE_AGE:
                raise ValueError("Expired power owner state")
            active = any(self.clock() < until for until in self.expires.values())
            if (
                session in self.closed or (active and session != self.session)
                or (session == self.session and sequence <= self.sequence)
                or issued < self.issued
            ):
                return False
            if self.session is not None and session != self.session:
                self.closed.append(self.session)
                self.states = {side: False for side in self.states}
                self.expires = {side: float("-inf") for side in self.states}
            self.session, self.sequence, self.issued = session, sequence, issued
            until = self.clock() + GRANT_SECONDS - max(0, self.wall_clock() - issued)
            for side, enabled in states.items():
                if enabled != self.states[side] or (enabled and not self.allowed(side)):
                    self.revisions[side] += 1
                self.states[side] = enabled
                self.expires[side] = until
            return True

    def snapshot(self):
        with self.lock:
            return {
                "enforced": True,
                "roles": {
                    side: {"requested_on": value, "color_allowed": self.allowed(side),
                           "revision": self.revisions[side],
                           "grant_remaining": max(0, self.expires[side] - self.clock())}
                    for side, value in self.states.items()
                },
            }
