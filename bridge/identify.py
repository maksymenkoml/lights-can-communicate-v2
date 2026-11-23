"""Blink one lamp on an owner request so the owner can find it.

Govee: two short brightness dips through the color sender, only while the lamp
is on, freshly observed and its color writer is open; the exact previous
brightness is restored. While a blink runs, the scene and Sync controllers'
writes to that light are rejected by ``wrap()`` (and retried after the blink,
because a rejected write leaves the controller's target unsent), so they never
race the dips. Other lights keep rendering.

While Sync drives a Govee lamp, the lamp is in DreamView and ignores a
brightness change until the next frame, so the blink is two white flashes in
DreamView frames instead (white, dark, white, dark) while Sync frames to that
light are held by the same fence; the next Sync frame restores the picture. A
single 0.3 s flash was easy to miss in a moving Sync picture; the owner saw
repeated ones. Only while the lamp received a DreamView frame within the last
two seconds.

Native Hue: the Hue API v2 ``identify`` action on the pinned light, only while
the light is on and freshly observed. While Sync drives the light it shows only
the Entertainment stream, so the blink is the same two white flashes on the
light's stream channels instead, only while frames keep coming.
"""

import contextlib
import math
import threading
import time

from desk_layout import GOVEE_ROWS, ROLES
from govee_segments import razer_data

import lights_can_communicate_v2 as lan

# The native Hue rows of the layout; one without a proxy is refused as unavailable.
NATIVE_ROWS = tuple(row for row in ROLES if row not in GOVEE_ROWS)

DIP_BRIGHTNESS = 10
STEP_SECONDS = 0.3
STATUS_SECONDS = 0.75
# A DreamView frame this recent shows the lamp is in DreamView and receiving.
DREAMVIEW_SECONDS = 2.0
DARK, WHITE = (0, 0, 0), (255, 255, 255)
NATIVE_WHITE = (65535, 65535, 65535)


class IdentifyRefused(RuntimeError):
    """The lamp cannot blink now (off, writer closed, Sync active or busy)."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class IdentifyFailed(RuntimeError):
    """A blink command was not delivered."""


class Identify:
    def __init__(self, controller, hue_proxies=None, video=None, power_owner=None,
                 layout_store=None, sleep=time.sleep):
        self.controller = controller
        self.hue_proxies = dict(hue_proxies or {})
        self.video, self.power_owner = video, power_owner
        self.layout_store = layout_store
        self.sleep = sleep
        # The color sender used for the dips; main() sets it to the gated sender
        # below the per-light blink fence.
        self.sender = None
        self.lock = threading.Lock()
        self.busy = set()
        self.fences = {light: threading.Lock() for light in controller.by_light}

    def wrap(self, sender):
        """Reject controller writes to a light while it blinks."""
        roles = {lan.device_key(device): light
                 for light, device in self.controller.by_light.items()}
        if self.sender is None:
            # Blinks bypass the fence they install.
            self.sender = sender

        def send(device, command, data):
            light = roles.get(lan.device_key(device))
            fence = self.fences.get(light)
            if fence is None:
                return sender(device, command, data)
            # The fence is held across the busy check and the send, and a blink
            # takes it before it starts, so no controller write lands inside it.
            with fence:
                with self.lock:
                    if light in self.busy:
                        return False
                return sender(device, command, data)

        return send

    def sync_owns(self, role):
        if self.video is None or not self.video.active():
            return False
        member = getattr(self.layout_store, "enabled", None)
        return member(role) if member is not None else True

    def __call__(self, role):
        govee = role in self.controller.by_light
        if not govee and role not in self.hue_proxies and role not in NATIVE_ROWS:
            raise ValueError("Unknown identify role")
        syncing = self.sync_owns(role)
        # Taking the light's fence waits for an in-flight controller write to finish.
        with self.fences.get(role) or contextlib.nullcontext():
            with self.lock:
                if role in self.busy:
                    raise IdentifyRefused("busy")
                self.busy.add(role)
        try:
            if syncing and govee:
                return self._govee_sync(role)
            if syncing:
                return self._native_sync(role)
            if govee:
                return self._govee(role)
            return self._native(role)
        finally:
            with self.lock:
                self.busy.discard(role)

    def _govee(self, light):
        controller = self.controller
        device = controller.by_light[light]
        with controller.lock:
            status, seen = controller.statuses.get(lan.device_key(device),
                                                   (None, float("-inf")))
            fresh = 0 <= controller.clock() - seen <= STATUS_SECONDS
        if not isinstance(status, dict) or status.get("onOff") != 1 or not fresh:
            raise IdentifyRefused("lamp off")
        brightness = status.get("brightness")
        if (type(brightness) not in (int, float) or not math.isfinite(brightness)
                or not 1 <= brightness <= 100):
            raise IdentifyRefused("brightness unknown")
        brightness = round(brightness)
        if self.power_owner is not None and not self.power_owner.allowed(light):
            raise IdentifyRefused("writer closed")
        sender = self.sender if self.sender is not None else controller.sender
        low = max(1, min(DIP_BRIGHTNESS, brightness // 3))
        delivered = True
        try:
            for value in (low, brightness, low):
                delivered = sender(device, "brightness", {"value": value}) is True and delivered
                self.sleep(STEP_SECONDS)
        except OSError:
            delivered = False
        finally:
            # Always end on the exact previous brightness.
            try:
                restored = sender(device, "brightness", {"value": brightness}) is True
            except OSError:
                restored = False
            with controller.lock:
                # The next render resends the controller's own target, if any.
                controller.last_sent.pop(light, None)
        if not (delivered and restored):
            raise IdentifyFailed("Blink was not delivered")
        return True

    def _govee_sync(self, light):
        sync = getattr(self.video, "controller", None)
        if sync is None or light not in sync.by_light:
            raise IdentifyRefused("sync active")
        device = sync.by_light[light]
        with sync.lock:
            frame_at = sync.razer_frame.get(light)
            target = sync.effective_target(light)
            dreamview = (light in sync.razer and frame_at is not None
                         and 0 <= sync.clock() - frame_at <= DREAMVIEW_SECONDS)
        segments = target.get("segments") if isinstance(target, dict) else None
        if not dreamview or not segments:
            raise IdentifyRefused("sync active")
        if self.power_owner is not None and not self.power_owner.allowed(light):
            raise IdentifyRefused("writer closed")
        sender = self.sender if self.sender is not None else sync.sender
        count = len(segments)
        delivered = True
        try:
            for color in (WHITE, DARK, WHITE, DARK):
                frame = razer_data([list(color)] * count)
                sent = sender(device, "razer", frame) is True
                delivered = sent and delivered
                if sent:
                    with sync.lock:
                        # Keeps DreamView fresh for the idle timer and a next blink.
                        sync.razer_frame[light] = sync.clock()
                self.sleep(STEP_SECONDS)
        except OSError:
            delivered = False
        finally:
            with sync.lock:
                # The next Sync frame is sent in full and restores the picture.
                sync.last_sent.pop(light, None)
        if not delivered:
            raise IdentifyFailed("Blink was not delivered")
        return True

    def _native_sync(self, role):
        relay = getattr(self.video, "relay", None)
        if relay is None or not relay.streams(role):
            raise IdentifyRefused("sync active")
        before = relay.sequence
        try:
            for color in (NATIVE_WHITE, DARK, NATIVE_WHITE, DARK):
                relay.override(role, color)
                self.sleep(STEP_SECONDS)
        finally:
            # The next stream frame shows the Sync colors again.
            relay.override(role, None)
        if relay.sequence == before:
            raise IdentifyFailed("Blink was not delivered")
        return True

    def _native(self, role):
        proxy = self.hue_proxies.get(role)
        if proxy is None:
            raise IdentifyRefused("unavailable")
        state = proxy.snapshot()
        age = state.get("native_age")
        if (state.get("healthy") is not True or state.get("native_on") is not True
                or type(age) not in (int, float) or not 0 <= age <= 2):
            raise IdentifyRefused("lamp off")
        try:
            proxy.client.identify()
        except (OSError, ValueError, KeyError) as error:
            raise IdentifyFailed("Identify was not confirmed") from error
        return True
