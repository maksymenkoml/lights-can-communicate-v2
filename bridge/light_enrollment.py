"""Owner enrollment and removal of discovered lights in the light registry.

The adapter is the only writer of the registry: it also records a lamp's new
address there. An enrollment or a removal is checked against the discovery
catalog at that moment (discovered, online, unambiguous, not a migrated lamp),
saved atomically, and then the adapter restarts so that every layer starts from
the new light set. Docker restarts the finished container. While Sync output is
active the light set is not changed.

A Hue light must also be a member of the paired Entertainment area: the owner
adds it there in the Hue app first, and its channels come from that area.
"""

import threading

from lights_can_communicate_v2_core.light_registry import (
    DEVICE_ID,
    MODEL_ID,
    RegistryError,
)
from lights_can_communicate_v2_core.native_profile import MAX_CHANNELS, MAX_LIGHTS


class LightRefused(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class LightEnrollment:
    def __init__(self, path, registry, catalog, busy, restart, area=None):
        self.path = path
        self.registry = registry
        self.catalog = catalog
        # The paired native Entertainment area (HueAreaReader), or None without one.
        self.area = area
        # True while Sync output owns the lamps.
        self.busy = busy
        # Called once after a changed registry is saved.
        self.restart = restart
        # Serializes light set changes with Sync frames (see stream), so Sync
        # cannot start between the busy check and the registry write.
        self.lock = threading.Lock()
        self.pending = False

    def _check(self, light_id):
        if type(light_id) is not str or DEVICE_ID.match(light_id) is None:
            raise ValueError("Invalid light id")
        if self.pending:
            raise LightRefused("restarting")
        if self.busy():
            raise LightRefused("sync_active")

    def _apply(self, registry):
        try:
            registry.save(self.path)
        except RegistryError:
            raise OSError("Light registry could not be written") from None
        self.registry = registry
        self.pending = True

    def enroll(self, light_id):
        """Add a discovered Govee or Hue light; raise LightRefused or ValueError."""
        member = None
        if self.catalog.hue_enrollment(light_id) is not None:
            with self.lock:
                self._check(light_id)
            # The area is read outside the lock, so Sync frames never wait for the bridge.
            member = self._hue_member(light_id)
        with self.lock:
            self._check(light_id)
            if light_id in self.registry.ids:
                raise LightRefused("enrolled")
            try:
                if member is not None:
                    # The light can go offline or ambiguous while the area is read.
                    if self.catalog.hue_enrollment(light_id) is None:
                        raise LightRefused("unavailable")
                    registry = self._with_hue_light(light_id, member)
                else:
                    binding = self.catalog.govee_enrollment(light_id)
                    if binding is None:
                        raise LightRefused("unavailable")
                    registry = self.registry.with_govee_light(light_id, **binding)
            except RegistryError:
                # The same device is already enrolled under another id.
                raise LightRefused("enrolled") from None
            self._apply(registry)
        self.restart()

    def _hue_member(self, light_id):
        hue = self.catalog.hue_enrollment(light_id)
        if hue is None:
            raise LightRefused("unavailable")
        if self.area is None:
            raise LightRefused("not_in_area")
        try:
            member = self.area.member(hue["light"])
        except (OSError, ValueError):
            raise LightRefused("unavailable") from None
        if member is None:
            raise LightRefused("not_in_area")
        model = hue["model"] if MODEL_ID.match(hue["model"]) else "unknown"
        return {**member, "light": hue["light"], "model": model}

    def _with_hue_light(self, light_id, member):
        profile = self.registry.native_profile()
        lights = profile.ids if profile is not None else ()
        channels = len(profile.expected_members) if profile is not None else 0
        if len(lights) >= MAX_LIGHTS or channels + member["segments"] > MAX_CHANNELS:
            raise LightRefused("area_full")
        return self.registry.with_hue_light(
            light_id, self.area.area_id, member["light"], member["service"],
            member["segments"], member["model"])

    def remove(self, light_id):
        """Remove an enrolled discovered light; its identity stays for a rejoin."""
        with self.lock:
            self._check(light_id)
            if light_id not in self.registry.ids:
                raise LightRefused("not_enrolled")
            self._apply(self.registry.without_light(light_id))
        self.restart()

    def stream(self, submit):
        """Pass a Sync frame to ``submit`` unless a new light set awaits the restart."""
        with self.lock:
            if self.pending:
                return False
            return submit()

    def remember(self, targets):
        """Record the lamps' current addresses; skipped once a new light set is saved."""
        with self.lock:
            if self.pending:
                return
            try:
                registry = self.registry.with_govee_targets(targets)
                registry.save(self.path)
            except RegistryError:
                raise OSError("Light registry could not be written") from None
            self.registry = registry
