"""Light Studio's Govee colour calibration session.

While the owner calibrates a Govee lamp, the lamp and a reference lamp show the
same test white and every other role lamp shows a dark colour, so the two can
be compared. The calibrated lamp's candidate slider values apply through the
transport calibration's override, unsaved. Scene writes to the involved lamps
are held and resent when the session ends. Power never changes: a lamp that is
off stays off, and dark means a black colour or minimum dimming.

A session needs Sync off: the Hue Entertainment stream holds the Hue lamps, so
the reference could not be set. It ends when Light Studio stops renewing it,
when Sync starts, or when the calibrated lamp or the reference turns off.
See ``docs/design/2026-11-19-govee-device-calibration.md``.
"""

import math
import threading
import time

from desk_layout import CALIBRATION_ROWS, ROLES, calibration_values
from govee_segments import razer_mode

import lights_can_communicate_v2 as lan

LEASE_SECONDS = 5
STATUS_SECONDS = 0.75
NATIVE_SECONDS = 2
# The cream white of Sync video, where the owner saw the lamps differ.
TEST_COLOR = (255, 235, 207)
LEVELS = {"bright": 100, "dim": 10}
DARK_HUE_DIMMING = 0.1
GOVEE_SIDES = ("left", "right")


class CalibrationRefused(RuntimeError):
    """The session cannot start or continue (reason is shown in Light Studio)."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _xy(rgb):
    """CIE xy of an sRGB colour (for the Hue reference)."""
    linear = [((c / 255 + 0.055) / 1.055) ** 2.4 if c / 255 > 0.04045 else c / 255 / 12.92
              for c in rgb]
    x = 0.4124 * linear[0] + 0.3576 * linear[1] + 0.1805 * linear[2]
    y = 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]
    z = 0.0193 * linear[0] + 0.1192 * linear[1] + 0.9505 * linear[2]
    total = x + y + z
    return {"x": round(x / total, 4), "y": round(y / total, 4)}


class CalibrationSession:
    def __init__(self, controller, calibration, hue_proxies=None, video=None,
                 power_owner=None, clock=time.monotonic):
        self.controller, self.calibration = controller, calibration
        self.hue_proxies = dict(hue_proxies or {})
        self.video, self.power_owner, self.clock = video, power_owner, clock
        # The Govee sender below the session fence; main() sets it.
        self.sender = None
        self.lock = threading.RLock()
        self.active = None
        self.last_end = None

    # -- fences -------------------------------------------------------------
    def wrap(self, sender):
        """Reject controller writes to the Govee lamps while a session runs."""
        sides = {lan.device_key(device): side
                 for side, device in self.controller.by_side.items()}
        if self.sender is None:
            self.sender = sender

        def send(device, command, data):
            with self.lock:
                if self.active is not None and sides.get(lan.device_key(device)):
                    return False
            return sender(device, command, data)

        return send

    def hold_native(self, base):
        """A Hue proxy write gate that also closes while a session runs."""
        return lambda: self.active is None and base()

    # -- observations -------------------------------------------------------
    def _sync_active(self):
        return self.video is not None and self.video.active()

    def _govee_on(self, side):
        controller = self.controller
        device = controller.by_side.get(side)
        if device is None:
            return False
        with controller.lock:
            status, seen = controller.statuses.get(lan.device_key(device), (None, float("-inf")))
            fresh = 0 <= controller.clock() - seen <= STATUS_SECONDS
        return isinstance(status, dict) and status.get("onOff") == 1 and fresh

    def _native_on(self, role):
        proxy = self.hue_proxies.get(role)
        if proxy is None:
            return False
        state = proxy.snapshot()
        age = state.get("native_age")
        return (state.get("native_on") is True and type(age) in (int, float)
                and math.isfinite(age) and 0 <= age <= NATIVE_SECONDS)

    def _on(self, role):
        return self._govee_on(role) if role in GOVEE_SIDES else self._native_on(role)

    # -- requests -----------------------------------------------------------
    def update(self, payload):
        """Start or renew the session; apply the outputs when anything changed."""
        if (not isinstance(payload, dict)
                or set(payload) != {"lamp", "reference", "level", "calibration"}):
            raise ValueError("Invalid calibration request")
        lamp, reference, level = payload["lamp"], payload["reference"], payload["level"]
        if lamp not in CALIBRATION_ROWS:
            raise ValueError("Only a Govee lamp can be calibrated")
        if reference not in ROLES or reference == lamp:
            raise ValueError("Invalid reference lamp")
        if level not in LEVELS:
            raise ValueError("Invalid test level")
        values = calibration_values(payload["calibration"])
        wanted = {"side": ROLES[lamp], "reference": ROLES[reference], "level": level,
                  "values": values}
        with self.lock:
            if self.active is not None and self.active["side"] != wanted["side"]:
                raise CalibrationRefused("busy")
            if self._sync_active():
                self._end("sync active")
                raise CalibrationRefused("sync active")
            if (self.power_owner is not None
                    and not self.power_owner.allowed(wanted["side"])):
                raise CalibrationRefused("writer closed")
            for role, reason in ((wanted["side"], "lamp off"),
                                 (wanted["reference"], "reference off")):
                if not self._on(role):
                    if self.active is not None:
                        self._end(reason)
                    raise CalibrationRefused(reason)
            starting = self.active is None
            changed = starting or any(self.active[key] != wanted[key] for key in wanted)
            self.active = {**wanted, "expires": self.clock() + LEASE_SECONDS}
            self.calibration.override(wanted["side"], values)
            if changed:
                self._apply(starting)
            return {"active": True}

    def end(self, reason="ended"):
        with self.lock:
            return self._end(reason)

    def tick(self):
        """End a session whose lease ran out, or that Sync or a power change ended."""
        with self.lock:
            if self.active is None:
                return
            if self.clock() > self.active["expires"]:
                self._end("expired")
            elif self._sync_active():
                self._end("sync active")
            elif not self._on(self.active["side"]):
                self._end("lamp off")
            elif not self._on(self.active["reference"]):
                self._end("reference off")

    def run(self, stop):
        while not stop.is_set():
            try:
                self.tick()
            except Exception:
                # A failed lamp must not stop the watcher; it retries.
                pass
            stop.wait(0.25)

    def snapshot(self):
        with self.lock:
            active = self.active
            return {"active": active is not None,
                    "side": active["side"] if active else None,
                    "reference": active["reference"] if active else None,
                    "last_end": self.last_end}

    # -- outputs ------------------------------------------------------------
    def _send(self, side, command, data):
        device = self.controller.by_side[side]
        try:
            return self.sender(device, command, data) is True
        except OSError:
            return False

    def _apply(self, starting):
        active = self.active
        level = LEVELS[active["level"]]
        for side in GOVEE_SIDES:
            if side not in self.controller.by_side or not self._govee_on(side):
                continue
            if starting:
                # DreamView ignores other colour commands; leave it first.
                self._send(side, "razer", razer_mode(False))
            lit = side in (active["side"], active["reference"])
            rgb = TEST_COLOR if lit else (0, 0, 0)
            self._send(side, "colorwc", {"color": dict(zip("rgb", rgb)),
                                         "colorTemInKelvin": 0})
            self._send(side, "brightness", {"value": level if lit else 1})
        for role, proxy in self.hue_proxies.items():
            if not self._native_on(role):
                continue
            body = ({"color": {"xy": _xy(TEST_COLOR)}, "dimming": {"brightness": level}}
                    if role == active["reference"]
                    else {"dimming": {"brightness": DARK_HUE_DIMMING}})
            try:
                with proxy.output_lock:
                    proxy.client.write(body)
            except (OSError, ValueError, KeyError):
                pass  # The lamp keeps its colour; the owner sees it.

    def _end(self, reason):
        active, self.active = self.active, None
        if active is None:
            return False
        self.last_end = reason
        self.calibration.override(active["side"], None)
        with self.controller.lock:
            for side in GOVEE_SIDES:
                # The next render resends each lamp's own scene.
                self.controller.last_sent.pop(side, None)
        for proxy in self.hue_proxies.values():
            proxy.restore_scene()
        return True
