"""Arbitrate scene/video output while preserving owner power and selected resources."""

import threading
import time

from entertainment_stream import GOVEE_CHANNELS, VideoLease
from stream_diagnostics import StreamDiagnostics
from sync_projection import project_govee_frame, project_native_frame


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
    def __init__(self, controller, relay, proxies, lease=None, clock=time.monotonic,
                 diagnostics=None):
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
        self.diagnostics = diagnostics or StreamDiagnostics()
        self.transitions = {}
        self.last_event = None
        self.current_profile = None
        self.native_membership = frozenset(("strip", "hue_left", "hue_center", "hue_right"))
        self.handoff = False

    def active(self):
        return (self.lease.active() or self.cleanup_pending
                or ((self.ready or self.handoff) and self.lease.holding()))

    def submit(self, message):
        return self.lease.submit(message)

    def selected_proxies(self):
        if self.current_profile is None:
            return self.proxies
        roles = set(self.current_profile["native_roles"])
        return [proxy for proxy in self.proxies if getattr(proxy, "role", None) in roles]

    def fresh_off(self):
        return any(s.get("healthy") and s.get("native_on") is False
                   and s.get("native_age") is not None and 0 <= s["native_age"] <= 2
                   for s in (proxy.snapshot() for proxy in self.selected_proxies()))

    def safe(self, starting=False):
        proxies = self.selected_proxies()
        snapshots = [proxy.snapshot() for proxy in proxies]
        complete = (len(snapshots) == 4 if self.current_profile is None else
                    {getattr(proxy, "role", None) for proxy in proxies}
                    == set(self.current_profile["native_roles"]))
        allowed = complete and all(
            s.get("healthy")
            and s.get("native_on") is True
            and s.get("native_age") is not None
            and 0 <= s["native_age"] <= 2
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
        self.handoff = False
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
            loader = getattr(self.controller.layout_store, "sync_profile", None)
            self.current_profile = loader() if loader is not None else None
            membership = (frozenset(self.current_profile["native_roles"])
                          if self.current_profile is not None else
                          frozenset(("strip", "hue_left", "hue_center", "hue_right")))
            if membership != self.native_membership:
                self.blocked_session = None
                self.next_retry = 0
                self.native_membership = membership
                if self.ready:
                    # Hold the video lease through the inactive-area handoff. Scene proxies
                    # stay fenced, including while fresh native normal-mode polls arrive.
                    self.handoff = True
                    self.ready = False
                    self.cleanup_pending = True
                    with self.lock:
                        self.relay.stop()
                    self.cleanup_pending = False
            for side in self.controller.by_side:
                if (self.current_profile is not None
                        and side not in self.current_profile["enabled_roles"]):
                    self.controller.clear_side(side)
            if self.cleanup_pending and not self.ready:
                self.release()
            if latest is None:
                if self.handoff and self.lease.holding():
                    self.last_result = "waiting for fresh frames; output held"
                    return False
                if self.handoff:
                    self.release()
                if self.ready and self.lease.holding():
                    if self.safe():
                        self.last_result = "waiting for fresh frames; output held"
                        return False
                    fresh_off = self.fresh_off()
                    if fresh_off:
                        self.blocked_session = self.lease.session
                    self.release()
                    self.next_retry = self.clock() + 1
                    self.last_result = ('native off; source restart required' if fresh_off
                                        else 'native state stale; waiting to retry')
                    return False
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
                    started = (self.relay.start(self.current_profile)
                               if self.current_profile is not None else self.relay.start())
                if not started:
                    self.cleanup_pending = False
                    self.last_result = "foreign native stream"
                    self.next_retry = self.clock() + 1
                    return False
                if not self.lease.active():
                    self.release()
                    return False
                self.ready = True
                self.handoff = False
                self.warmup_until = self.clock() + 2
            latest = self.lease.latest()
            if latest is None:
                self.release()
                return False
            session, sequence, frame = latest
            if not self.safe():
                fresh_off = self.fresh_off()
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
            native_frame = (project_native_frame(frame, self.current_profile)
                            if self.current_profile is not None else frame)
            self.relay.send(native_frame)
            for side, channels in GOVEE_CHANNELS.items():
                if self.current_profile is not None:
                    if side not in self.current_profile["enabled_roles"]:
                        continue
                    stops = project_govee_frame(frame, side, self.current_profile)
                    if self.controller.by_side[side].get("segment_count", 0) == 7:
                        state = govee_stream_state(stops[3], stops)
                    else:
                        visible = [color for color, sample in zip(
                            stops, self.current_profile["govee"][side]["samples"])
                            if sample is not None]
                        representative = visible[len(visible) // 2] if visible else (0, 0, 0)
                        state = govee_stream_state(representative)
                else:
                    state = govee_stream_state(
                        frame[channels[0]],
                        [frame[channel] for channel in channels] if len(channels) > 1 else None,
                    )
                self.controller.submit(
                    {
                        "side": side,
                        "session": session,
                        "sequence": sequence,
                        "issued_at": self.controller.wall_clock(),
                        "transitiontime": 0,
                        "state": state,
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
        finally:
            self.observe()

    def observe(self):
        """Record bounded transitions, never raw exceptions or resource identifiers."""
        try:
            result = self.last_result
            lease = self.lease.snapshot()
            if result == 'streaming':
                state, reason = 'streaming', 'frames_received'
            elif result == 'waiting for fresh frames; output held':
                state, reason = 'holding', 'frame_gap'
            elif result == 'idle':
                state, reason = (
                    ('stopped', 'shutdown') if self.lease.stopping else
                    ('waiting', 'starting') if not lease['frames_received'] else
                    ('idle', 'explicit_stop' if self.lease.session is None else 'grace_elapsed'))
            elif result == 'native relay unavailable':
                state, reason = 'unavailable', 'relay_error'
            elif result == 'foreign native stream':
                state, reason = 'waiting', 'native_foreign'
            elif result == 'native off; source restart required':
                state, reason = 'stopped', 'native_off'
            else:
                state, reason = 'waiting', 'native_unavailable'
            key = (state, reason)
            if key != self.last_event:
                self.transitions[reason] = self.transitions.get(reason, 0) + 1
                self.last_event = key
            self.diagnostics.record(state, reason, {
                'frame_age': lease['frame_age'], 'frames_received': lease['frames_received'],
                'frames_sent': self.frames_sent,
            })
        except Exception:
            pass  # Passive diagnostics must not change rendering or recovery.

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
            "cleanup_pending": self.cleanup_pending and not self.ready,
            "native_owned": bool(getattr(self.relay, "owned", False)),
            "frames_sent": self.frames_sent,
            "last_result": self.last_result,
            "last_safety_failure": self.last_safety_failure,
            "govee": self.controller.snapshot(),
            "transitions": dict(self.transitions),
            "diagnostics": self.diagnostics.snapshot(),
        }
