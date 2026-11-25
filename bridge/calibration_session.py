"""Light Studio's lamp calibration session.

While the owner calibrates a role lamp, the lamp and a reference lamp show the
same test white and every other role lamp shows a dark colour, so the two can
be compared. Each shows the test level times its brightness correction for the
configuration being edited: the calibrated lamp the candidate value, the
reference its saved one. A Govee lamp's candidate colour values apply through
the transport calibration's override, unsaved. Scene writes to the involved
lamps are held and resent when the session ends. Power never changes: a lamp
that is off stays off, and dark means a black colour or minimum dimming.

A session needs Sync off: the Hue Entertainment stream holds the Hue lamps, so
the reference could not be set. It ends when Light Studio stops renewing it,
when Sync starts, or when the calibrated lamp or the reference turns off.
See ``docs/design/2026-11-19-govee-device-calibration.md``.
"""

import math
import threading
import time

from desk_layout import calibration_values
from govee_segments import razer_mode

import lights_can_communicate_v2 as lan

LEASE_SECONDS = 5
STATUS_SECONDS = 0.75
NATIVE_SECONDS = 2
# The cream white of Sync video, where the owner saw the lamps differ.
TEST_COLOR = (255, 235, 207)
LEVELS = {"bright": 100, "dim": 10}
VIEWS = ("sync", "scene")
DARK_HUE_DIMMING = 0.1
# A lit test level never drops to what reads as off.
LIT_HUE_DIMMING = 0.5


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
        # A scene write that reached a lamp after the session's test white.
        self.stale = False

    # -- fences -------------------------------------------------------------
    def wrap(self, sender):
        """Reject controller writes to the Govee lamps while a session runs."""
        lights = {lan.device_key(device): light
                  for light, device in self.controller.by_light.items()}
        if self.sender is None:
            self.sender = sender

        def send(device, command, data):
            # No session lock here: the controller sends under its own lock, and a
            # session reads the controller while it holds the session lock, so taking
            # both in opposite orders deadlocks the adapter. Reading ``active`` once is
            # enough; a write that races a starting session is painted over by tick().
            light = lights.get(lan.device_key(device))
            if light and self.active is not None:
                return False
            result = sender(device, command, data)
            if light and self.active is not None:
                self.stale = True
            return result

        return send

    def hold_native(self, base):
        """A Hue proxy write gate that also closes while a session runs."""
        return lambda: self.active is None and base()

    # -- observations -------------------------------------------------------
    def _sync_active(self):
        return self.video is not None and self.video.active()

    def _govee_on(self, light):
        controller = self.controller
        device = controller.by_light.get(light)
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
        return (self._govee_on(role) if role in self.controller.by_light
                else self._native_on(role))

    def _saved_correction(self, role, view):
        """The reference's saved correction (0-100) for ``view``."""
        store = getattr(self.controller, "layout_store", None)
        reader = getattr(store, "scale" if view == "sync" else "scene_scale", None)
        return 100 * reader(role, 1.0) if reader is not None else 100

    # -- requests -----------------------------------------------------------
    def _role(self, lamp):
        """The session key of a layout row: its Govee or native Hue light id."""
        return lamp if lamp in self.controller.by_light or lamp in self.hue_proxies else None

    def update(self, payload):
        """Start or renew the session; apply the outputs when anything changed."""
        if (not isinstance(payload, dict) or not isinstance(payload.get("lamp"), str)
                or self._role(payload["lamp"]) is None):
            raise ValueError("Invalid calibration request")
        lamp = payload["lamp"]
        govee = lamp in self.controller.by_light
        keys = {"lamp", "reference", "level", "view", "correction"}
        if set(payload) != (keys | {"calibration"} if govee else keys):
            raise ValueError("Invalid calibration request")
        reference, level, view = payload["reference"], payload["level"], payload["view"]
        if (not isinstance(reference, str) or self._role(reference) is None
                or reference == lamp):
            raise ValueError("Invalid reference lamp")
        if level not in LEVELS or view not in VIEWS:
            raise ValueError("Invalid test level")
        correction = payload["correction"]
        if type(correction) is not int or not 0 <= correction <= 100:
            raise ValueError("Invalid correction")
        values = calibration_values(payload["calibration"]) if govee else None
        wanted = {"role": self._role(lamp), "reference": self._role(reference), "level": level,
                  "view": view, "correction": correction, "values": values}
        with self.lock:
            if self.active is not None and self.active["role"] != wanted["role"]:
                raise CalibrationRefused("busy")
            if self._sync_active():
                self._end("sync active")
                raise CalibrationRefused("sync active")
            if (govee and self.power_owner is not None
                    and not self.power_owner.allowed(wanted["role"])):
                raise CalibrationRefused("writer closed")
            for role, reason in ((wanted["role"], "lamp off"),
                                 (wanted["reference"], "reference off")):
                if not self._on(role):
                    if self.active is not None:
                        self._end(reason)
                    raise CalibrationRefused(reason)
            starting = self.active is None
            changed = starting or any(self.active[key] != wanted[key] for key in wanted)
            if starting:
                self.stale = False
                for proxy in self.hue_proxies.values():
                    # A Hue light gets back what it shows now when the session ends.
                    proxy.remember_light()
            self.active = {**wanted, "expires": self.clock() + LEASE_SECONDS}
            if govee:
                self.calibration.override(wanted["role"], values)
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
            elif not self._on(self.active["role"]):
                self._end("lamp off")
            elif not self._on(self.active["reference"]):
                self._end("reference off")
            elif self.stale:
                self.stale = False
                self._apply(False)

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
                    "lamp": active["role"] if active else None,
                    "reference": active["reference"] if active else None,
                    "last_end": self.last_end}

    # -- outputs ------------------------------------------------------------
    def _send(self, light, command, data):
        device = self.controller.by_light[light]
        try:
            return self.sender(device, command, data) is True
        except OSError:
            return False

    def _levels(self):
        """Test level (0-100) per lit role: the lamp's candidate, the reference's saved."""
        active = self.active
        level = LEVELS[active["level"]]
        saved = self._saved_correction(active["reference"], active["view"])
        return {active["role"]: level * active["correction"] / 100,
                active["reference"]: level * saved / 100}

    def _apply(self, starting):
        lit = self._levels()
        for light in self.controller.by_light:
            if not self._govee_on(light):
                continue
            if starting:
                # DreamView ignores other colour commands; leave it first.
                self._send(light, "razer", razer_mode(False))
            rgb = TEST_COLOR if light in lit else (0, 0, 0)
            self._send(light, "colorwc", {"color": dict(zip("rgb", rgb)),
                                          "colorTemInKelvin": 0})
            self._send(light, "brightness",
                       {"value": max(1, round(lit[light])) if light in lit else 1})
        for role, proxy in self.hue_proxies.items():
            if not self._native_on(role):
                continue
            body = ({"color": {"xy": _xy(TEST_COLOR)},
                     "dimming": {"brightness": max(LIT_HUE_DIMMING, round(lit[role], 1))}}
                    if role in lit else {"dimming": {"brightness": DARK_HUE_DIMMING}})
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
        if active["values"] is not None:
            self.calibration.override(active["role"], None)
        with self.controller.lock:
            for light in self.controller.by_light:
                # The next render resends each lamp's own scene.
                self.controller.last_sent.pop(light, None)
        for proxy in self.hue_proxies.values():
            proxy.restore_scene()
        return True
