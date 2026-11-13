"""Arbitrate scene/video output while preserving owner power and selected resources."""

import threading
import time

from entertainment_stream import GOVEE_CHANNELS, VideoLease


def govee_stream_state(rgb, stops=None):
    state = {
        "on": True,
        "colormode": "rgb",
        "rgb": [round(v / 65535 * 255) for v in rgb],
        "bri": 254,
    }
    if stops is not None:
        state["segments"] = [[round(value / 65535 * 255) for value in color] for color in stops]
    return state


class VideoOutput:
    def __init__(self, controller, relay, proxies, lease=None, clock=time.monotonic):
        self.controller, self.relay, self.proxies = controller, relay, list(proxies)
        self.lease = lease or VideoLease()
        self.ready = False
        self.lock = threading.RLock()
        self.last_result = "waiting"
        self.frames_sent = 0
        self.blocked_session = None
        self.next_retry = 0
        self.cleanup_pending = False
        self.last_safety_failure = None
        self.clock = clock
        self.warmup_until = 0

    def active(self):
        return self.lease.active() or self.cleanup_pending

    def submit(self, message):
        return self.lease.submit(message)

    def safe(self, starting=False):
        snapshots = [proxy.snapshot() for proxy in self.proxies]
        allowed = len(snapshots) == 4 and all(
            s.get("healthy")
            and s.get("native_on") is True
            and s.get("native_age") is not None
            and s["native_age"] <= 2
            and s.get("native_mode") in (("normal",) if starting else ("normal", "streaming"))
            for s in snapshots
        )
        if not allowed:
            self.last_safety_failure = {
                "phase": "start" if starting else "stream",
                "native": [
                    {
                        key: s.get(key)
                        for key in (
                            "resource_id",
                            "healthy",
                            "native_on",
                            "native_mode",
                            "native_age",
                            "last_result",
                        )
                    }
                    for s in snapshots
                ],
            }
        return allowed

    def release(self):
        self.ready = False
        with self.lock:
            self.relay.stop()
        self.cleanup_pending = False
        with self.controller.lock:
            self.controller.jobs.clear()
            self.controller.last_sent.clear()
        for proxy in self.proxies:
            proxy.restore_scene()

    def process_once(self):
        latest = self.lease.latest()
        try:
            if self.cleanup_pending and not self.ready:
                self.release()
            if latest is None:
                if self.ready or self.cleanup_pending:
                    self.release()
                self.last_result = "idle"
                return False
            session, sequence, frame = latest
            if self.blocked_session == session:
                return False
            if not self.ready:
                if self.clock() < self.next_retry:
                    return False
                if self.cleanup_pending:
                    self.release()
                if not self.safe(starting=True):
                    self.last_result = "waiting for native on/normal"
                    return False
                with self.lock:
                    self.cleanup_pending = True
                    started = self.relay.start()
                if not started:
                    self.cleanup_pending = False
                    self.last_result = "foreign native stream"
                    self.next_retry = self.clock() + 1
                    return False
                if not self.lease.active():
                    self.release()
                    return False
                self.ready = True
                self.warmup_until = self.clock() + 2
            latest = self.lease.latest()
            if latest is None:
                self.release()
                return False
            session, sequence, frame = latest
            if not self.safe():
                fresh_off = any(
                    s.get("healthy")
                    and s.get("native_on") is False
                    and s.get("native_age") is not None
                    and s["native_age"] <= 2
                    for s in (proxy.snapshot() for proxy in self.proxies)
                )
                if not fresh_off and self.clock() < self.warmup_until:
                    self.last_result = "waiting for fresh native state after handshake"
                    return False
                if fresh_off:
                    self.blocked_session = session
                self.release()
                self.next_retry = self.clock() + 1
                self.last_result = (
                    "native off; source restart required"
                    if fresh_off
                    else "native state stale; waiting to retry"
                )
                return False
            self.relay.send(frame)
            for side, channels in GOVEE_CHANNELS.items():
                self.controller.submit(
                    {
                        "side": side,
                        "session": session,
                        "sequence": sequence,
                        "issued_at": self.controller.wall_clock(),
                        "transitiontime": 0,
                        "state": govee_stream_state(
                            frame[channels[0]],
                            [frame[channel] for channel in channels] if len(channels) > 1 else None,
                        ),
                    }
                )
            self.frames_sent += 1
            self.last_result = "streaming"
            return True
        except (OSError, ValueError, KeyError, IndexError, TypeError):
            self.ready = False
            try:
                self.release()
            except (OSError, ValueError, KeyError, IndexError, TypeError):
                pass
            self.next_retry = self.clock() + 1
            self.last_result = "native relay unavailable"
            return False

    def render(self):
        with self.lock:
            if self.ready and self.lease.active() and self.safe():
                self.controller.render()

    def run(self, stop):
        try:
            self.relay.recover()
        except (OSError, ValueError, KeyError, IndexError, TypeError):
            self.cleanup_pending = True
        while not stop.is_set():
            self.process_once()
            stop.wait(0.05)
        for attempt in range(3):
            try:
                self.release()
                break
            except (OSError, ValueError, KeyError):
                if attempt < 2:
                    time.sleep(0.2)

    def snapshot(self):
        return {
            **self.lease.snapshot(),
            "ready": self.ready,
            "frames_sent": self.frames_sent,
            "last_result": self.last_result,
            "last_safety_failure": self.last_safety_failure,
            "govee": self.controller.snapshot(),
        }
