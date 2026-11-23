"""Owner enrollment and removal of discovered lights in the light registry.

The adapter is the only writer of the registry: it also records a lamp's new
address there. An enrollment or a removal is checked against the discovery
catalog at that moment (discovered, online, unambiguous, not a migrated lamp),
saved atomically, and then the adapter restarts so that every layer starts from
the new light set. Docker restarts the finished container. While Sync output is
active the light set is not changed.

A Hue light is also added to the paired Entertainment area, and its channels
come from that area; a removal takes it out again. The area is changed before
the registry, and an enrollment that is refused after the change undoes it.
"""

import threading

from hue_area import AreaBusy, AreaShared

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
        # The paired native Entertainment area (HueArea), or None without one.
        self.area = area
        # True while Sync output owns the lamps.
        self.busy = busy
        # Called once after a changed registry is saved.
        self.restart = restart
        # Serializes light set changes with Sync frames (see stream), so Sync
        # cannot start between the busy check and the registry write.
        self.lock = threading.Lock()
        # Serializes whole enrollments and removals, including the area changes on the
        # bridge that run outside ``lock``.
        self.changes = threading.Lock()
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
        with self.changes:
            self._enroll(light_id)

    def _enroll(self, light_id):
        if self.catalog.hue_enrollment(light_id) is not None:
            self._enroll_hue(light_id)
            return
        with self.lock:
            self._check(light_id)
            if light_id in self.registry.ids:
                raise LightRefused("enrolled")
            binding = self.catalog.govee_enrollment(light_id)
            if binding is None:
                raise LightRefused("unavailable")
            try:
                registry = self.registry.with_govee_light(light_id, **binding)
            except RegistryError:
                # The same device is already enrolled under another id.
                raise LightRefused("enrolled") from None
            self._apply(registry)
        self.restart()

    def _enroll_hue(self, light_id):
        with self.lock:
            self._check(light_id)
            self._hue_check(light_id)
            hue = self.catalog.hue_enrollment(light_id)
            if hue is None:
                raise LightRefused("unavailable")
        # The bridge is called outside the lock, so Sync frames never wait for it.
        member, added = self._area_call(self.area.join, hue["light"])
        try:
            with self.lock:
                self._check(light_id)
                self._hue_check(light_id)
                # The light can go offline or ambiguous while the area changes.
                if self.catalog.hue_enrollment(light_id) is None:
                    raise LightRefused("unavailable")
                model = hue["model"] if MODEL_ID.match(hue["model"]) else "unknown"
                self._apply(self._with_hue_light(light_id, {**member, "light": hue["light"],
                                                            "model": model}))
        except BaseException:
            if added:
                try:
                    self.area.leave(hue["light"])
                except (AreaBusy, AreaShared, OSError, ValueError):
                    pass  # The relay refuses the unknown member until it is enrolled.
            raise
        self.restart()

    def _hue_check(self, light_id):
        if light_id in self.registry.ids:
            raise LightRefused("enrolled")
        if self.area is None:
            raise LightRefused("unavailable")
        profile = self.registry.native_profile()
        if profile is not None and len(profile.ids) >= MAX_LIGHTS:
            raise LightRefused("area_full")

    def _area_call(self, call, light):
        try:
            return call(light)
        except AreaBusy:
            raise LightRefused("sync_active") from None
        except AreaShared:
            raise LightRefused("channels_shared") from None
        except (OSError, ValueError):
            raise LightRefused("unavailable") from None

    def _with_hue_light(self, light_id, member):
        profile = self.registry.native_profile()
        lights = profile.ids if profile is not None else ()
        channels = len(profile.expected_members) if profile is not None else 0
        if len(lights) >= MAX_LIGHTS or channels + member["segments"] > MAX_CHANNELS:
            raise LightRefused("area_full")
        try:
            return self.registry.with_hue_light(
                light_id, self.area.area_id, member["light"], member["service"],
                member["segments"], member["model"])
        except RegistryError:
            # The same device is already enrolled under another id.
            raise LightRefused("enrolled") from None

    def _native_light(self, light_id):
        for item in self.registry.to_dict()["lights"]:
            if item["id"] == light_id and item.get("provider") == "hue":
                return item["hue"]["light"]
        return None

    def remove(self, light_id):
        """Remove an enrolled discovered light; its identity stays for a rejoin.

        A Hue light leaves the paired area first, so the relay never meets an
        area member that is not enrolled; a refused removal puts it back."""
        with self.changes:
            self._remove(light_id)

    def _remove(self, light_id):
        with self.lock:
            self._check(light_id)
            if light_id not in self.registry.ids:
                raise LightRefused("not_enrolled")
            native = self._native_light(light_id)
        before = None
        if native is not None:
            if self.area is None:
                raise LightRefused("unavailable")
            before = self._area_call(self.area.leave, native)
        try:
            with self.lock:
                self._check(light_id)
                if light_id not in self.registry.ids:
                    raise LightRefused("not_enrolled")
                self._apply(self.registry.without_light(light_id))
        except BaseException:
            if before is not None:
                try:
                    self.area.rejoin(before)
                except (OSError, ValueError):
                    pass  # The relay restores an enrolled member from its location cache.
            raise
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
