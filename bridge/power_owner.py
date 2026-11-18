"""Opt-in, short-lived owner permission for the two pinned Govee color writers."""

import math
import threading
import time
from collections import deque

from power_history import PowerHistory

import lights_can_communicate_v2 as lan

GRANT_SECONDS = 2.0
MAX_MESSAGE_AGE = 0.5
# A Sync-hold power command waits this long for a fresh native reading.
HOLD_CONFIRM_SECONDS = 3.0
HOLD_RESEND_SECONDS = 1.0


def switch_power(send, observe, side, on, clock, sleep):
    """Send a Govee ``turn`` and wait for a fresh native reading; resend once.

    ``send(on)`` delivers the command and returns the time it was sent. Returns
    True only when ``observe()`` reports the requested state from a sample taken
    after the command.
    """
    started = send(on)
    resent = False
    while clock() - started < HOLD_CONFIRM_SECONDS:
        observed = observe().get(side, {})
        sample = observed.get("sample")
        if observed.get("on") is on and type(sample) in (int, float) and sample > started:
            return True
        if not resent and clock() - started >= HOLD_RESEND_SECONDS:
            resent = True
            send(on)
        sleep(0.05)
    return False


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
        # Sync holds (sync_off.py): side -> owner state seen at the hold, or None.
        # A hold is not owner intent; it only closes the color writer.
        self.devices = {device["side"]: device for device in targets}
        self.holds = {}
        self.power_sender = self.observe = None
        self.sleep = time.sleep
        self.history = PowerHistory(clock, wall_clock)

    def _expired(self):
        """Record grants that lapsed since the last heartbeat, at their expiry time."""
        now = self.clock()
        for side in self.states:
            last = self.history.gate(side)
            if last is not None and last[1] and now >= self.expires[side]:
                self.history.record(side, "grant_expired", self.states[side], False,
                                    side in self.holds, at=self.expires[side])

    def _record(self, side, cause):
        """Record the published gate after a change; the caller holds the lock."""
        self._expired()
        self.history.record(side, cause, self.states[side], self.allowed(side),
                            side in self.holds)

    def attach_power(self, sender, observe, sleep=time.sleep):
        """Give Sync holds an unwrapped ``turn`` path and native observations."""
        self.power_sender, self.observe, self.sleep = sender, observe, sleep

    def revision(self, side):
        with self.lock:
            return self.revisions.get(side, 0)

    def allowed(self, side):
        with self.lock:
            return (self.states.get(side) is True and side not in self.holds
                    and self.clock() < self.expires.get(side, 0))

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
            self._expired()
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
                if side in self.holds:
                    if self.holds[side] is None:
                        self.holds[side] = enabled
                    elif self.holds[side] is not enabled:
                        # The owner changed this lamp during Sync: the owner's choice wins.
                        del self.holds[side]
                        self.revisions[side] += 1
            for side in self.states:
                self._record(side, "owner")
            return True

    # Sync hold ------------------------------------------------------------

    def _send_turn(self, side, on):
        with self.lock:
            sent = self.clock()
            self.power_sender(self.devices[side], "turn", {"value": 1 if on else 0})
            return sent

    def sync_hold(self, side, send=True):
        """Switch a lamp off for Sync; return whether it was on and is now held.

        Without ``send`` (crash recovery) the hold is placed without a command.
        """
        if side not in self.states:
            raise ValueError("Unknown Sync hold role")
        if send and (self.power_sender is None or self.observe is None):
            return False
        # Observe before taking the gate lock: render() takes them in the other order.
        native = self.observe().get(side, {}).get("on") if send else None
        with self.lock:
            if side in self.holds:
                return True
            if send:
                active = self.clock() < self.expires[side]
                if (native if native is not None else active and self.states[side]) is not True:
                    return False
            self.holds[side] = (self.states[side]
                                if self.clock() < self.expires[side] else None)
            self.revisions[side] += 1
            self._record(side, "sync_hold")
        if send:
            switch_power(lambda on: self._send_turn(side, on), self.observe, side, False,
                         self.clock, self.sleep)
        return True

    def sync_held(self, side):
        with self.lock:
            return side in self.holds

    def sync_release(self, side):
        """Restore On after Sync unless the owner changed the lamp.

        Returns "restored", "superseded" or "failed"; a failed restore keeps the
        hold, so the session can retry it.
        """
        with self.lock:
            if side not in self.holds:
                return "superseded"
            held = self.holds.pop(side)
            self.revisions[side] += 1
            self._record(side, "sync_release")
            # A connected owner that now wants the lamp off keeps it off.
            owner_off = self.clock() < self.expires[side] and self.states[side] is False
        if owner_off or self.power_sender is None or self.observe is None:
            return "superseded"
        restored = False
        try:
            restored = switch_power(lambda on: self._send_turn(side, on), self.observe, side,
                                    True, self.clock, self.sleep)
        finally:
            if not restored:
                with self.lock:
                    self.holds[side] = held
                    self.revisions[side] += 1
                    self._record(side, "sync_restore_failed")
        return "restored" if restored else "failed"

    def owner_power(self, side):
        """An explicit owner power request for this lamp ends its Sync hold."""
        with self.lock:
            if side in self.holds:
                del self.holds[side]
                self.revisions[side] += 1
                self._record(side, "owner_power")

    def snapshot(self):
        with self.lock:
            self._expired()
            return {
                "enforced": True,
                "events": self.history.snapshot(),
                "roles": {
                    side: {"requested_on": value, "color_allowed": self.allowed(side),
                           "revision": self.revisions[side],
                           "sync_hold": side in self.holds,
                           "grant_remaining": max(0, self.expires[side] - self.clock())}
                    for side, value in self.states.items()
                },
            }
