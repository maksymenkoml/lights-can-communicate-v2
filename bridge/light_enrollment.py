"""Owner enrollment and removal of lights in the light registry.

The adapter is the only writer of the registry: it also records a lamp's new
address there. An enrollment is checked against the discovery catalog at that
moment (discovered, online, unambiguous, not an enrolled migrated lamp). Any
enrolled light can be removed, the six migrated lamps too; a removed migrated
lamp that is enrolled again gets its migrated id back (see light_registry). A
change is saved atomically, and then the adapter restarts so that every layer
starts from the new light set. Docker restarts the finished container. While Sync
output is active the light set is not changed.

A Hue light of the Sync Box's room is also added to the paired Entertainment
area, and its channels come from that area; a removal takes it out again. A Hue
light of another room, or one that the area cannot take (shared channels, a full
area), is connected for scenes only, outside the area (#78). ``follow_rooms``
moves the Hue lights into or out of the area when the rooms change. The area is
changed before the registry, and a change that is refused after it undoes it.
"""

import threading

from hue_area import AreaBusy, AreaShared

from lights_can_communicate_v2_core.light_registry import (
    DEVICE_ID,
    LIGHT_ID,
    MODEL_ID,
    RegistryError,
)
from lights_can_communicate_v2_core.native_profile import MAX_CHANNELS, MAX_LIGHTS


class LightRefused(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class LightEnrollment:
    def __init__(self, path, registry, catalog, busy, restart, area=None, rooms=None):
        self.path = path
        # The room index (rooms.Rooms): a Hue light is in the area only in the Sync
        # Box's room. Without it, every light is in that room.
        self.rooms = rooms
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

    def _check(self, light_id, pattern=DEVICE_ID):
        if type(light_id) is not str or pattern.match(light_id) is None:
            raise ValueError("Invalid light id")
        self._check_state()

    def _check_state(self):
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
        """Add a discovered Govee or Hue light and return its registry id.

        The id is ``light_id``, or the migrated id of a migrated lamp that rejoins.
        Raise LightRefused or ValueError."""
        with self.changes:
            self._enroll(light_id)
            # A light is appended last (see light_registry._append).
            return self.registry.ids[-1]

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

    def _in_sync_room(self, light_id):
        if self.rooms is None:
            return True
        sync = self.rooms.sync_room()
        return sync is not None and self.rooms.room_of(light_id) == sync

    def _hue_ready(self, light_id):
        """Check a Hue enrollment under ``lock``; return the catalog's binding."""
        self._check(light_id)
        if light_id in self.registry.ids:
            raise LightRefused("enrolled")
        # Without usable paired credentials there is no area client. A light for
        # scenes only needs them as well: the adapter drives it with the paired key
        # and refuses to start with a Hue light but no Hue configuration. A paired
        # bridge without an area still has a client (its area id is None).
        if self.area is None:
            raise LightRefused("unavailable")
        hue = self.catalog.hue_enrollment(light_id)
        if hue is None:
            raise LightRefused("unavailable")
        return hue

    def _fits(self, registry, segments):
        """Whether the area of ``registry`` has room for a light of ``segments``."""
        profile = registry.native_profile()
        if profile is None:
            return segments <= MAX_CHANNELS
        return (len(profile.ids) < MAX_LIGHTS
                and len(profile.expected_members) + segments <= MAX_CHANNELS)

    def _join(self, light, registry):
        """Join ``light`` to the area. Return ``(member, added, None)``, or
        ``(None, False, reason)`` when the area cannot take it."""
        profile = registry.native_profile()
        if profile is not None and len(profile.ids) >= MAX_LIGHTS:
            return None, False, "area_full"
        # Without a light in the area, an area that the bridge lost can be replaced.
        try:
            member, added = self._area_call(
                lambda native: self.area.join(native, replace_missing=profile is None), light)
        except LightRefused as refused:
            if refused.reason != "channels_shared":
                raise
            return None, False, "channels_shared"
        if not self._fits(registry, member["segments"]):
            self._undo_join(light, added)
            return None, False, "area_full"
        return member, added, None

    def _undo_join(self, light, added):
        # A light that was a member before is left there.
        if not added:
            return
        try:
            self.area.abandon(light)
        except (AreaBusy, AreaShared, OSError, ValueError):
            pass  # The relay refuses the unknown member until it is enrolled.

    def _enroll_hue(self, light_id):
        with self.lock:
            hue = self._hue_ready(light_id)
        model = hue["model"] if MODEL_ID.match(hue["model"]) else "unknown"
        # The bridge is called outside the lock, so Sync frames never wait for it.
        member, added, reason = None, False, "room"
        if self._in_sync_room(light_id):
            member, added, reason = self._join(hue["light"], self.registry)
        try:
            with self.lock:
                self._hue_ready(light_id)
                if member is not None:
                    registry = self.registry.with_hue_light(
                        light_id, self.area.area_id, hue["light"], member["service"],
                        member["segments"], model)
                else:
                    registry = self.registry.with_scene_hue_light(
                        light_id, hue["light"], model, reason)
                self._apply(registry)
        except RegistryError:
            if member is not None:
                self._undo_join(hue["light"], added)
            # The same device is already enrolled under another id.
            raise LightRefused("enrolled") from None
        except BaseException:
            if member is not None:
                self._undo_join(hue["light"], added)
            raise
        self.restart()

    def _area_call(self, call, light):
        try:
            return call(light)
        except AreaBusy:
            raise LightRefused("sync_active") from None
        except AreaShared:
            raise LightRefused("channels_shared") from None
        except (OSError, ValueError):
            raise LightRefused("unavailable") from None

    def follow_rooms(self):
        """Move the Hue lights into or out of the area so that it holds the Hue
        lights of the Sync Box's room that it can take. Return ``{light id: reason
        or None}`` of the lights that changed: None for a light that joined the
        area, else why it is outside now. Raise LightRefused; a refused change
        undoes the area changes made before it."""
        with self.changes:
            with self.lock:
                self._check_state()
                registry = self.registry
                if self.area is None:
                    # Without a Hue light there is nothing to move.
                    if registry.hue_lights() is None:
                        return {}
                    raise LightRefused("unavailable")
            hue = [(item["id"], item["hue"]) for item in registry.to_dict()["lights"]
                   if item["provider"] == "hue"]
            changes, left, joined = {}, None, []
            try:
                # Leaving first frees channels for the lights that join.
                for light_id, item in hue:
                    if self._in_sync_room(light_id) or item.get("outside") == "room":
                        continue
                    if item["channels"]:
                        before = self._area_call(self.area.leave, item["light"])
                        left = left if left is not None else before
                    registry = registry.with_outside(light_id, "room")
                    changes[light_id] = "room"
                for light_id, item in hue:
                    if item["channels"] or not self._in_sync_room(light_id):
                        continue
                    member, added, reason = self._join(item["light"], registry)
                    if member is None:
                        if reason != item["outside"]:
                            registry = registry.with_outside(light_id, reason)
                            changes[light_id] = reason
                        continue
                    joined.append((item["light"], added))
                    registry = registry.with_area_member(
                        light_id, self.area.area_id, member["service"], member["segments"])
                    changes[light_id] = None
                with self.lock:
                    self._check_state()
                    if changes:
                        self._apply(registry)
            except BaseException:
                for light, added in reversed(joined):
                    self._undo_join(light, added)
                if left is not None:
                    try:
                        self.area.rejoin(left)
                    except (OSError, ValueError):
                        pass  # The relay restores an enrolled member from its location cache.
                raise
        if changes:
            self.restart()
        return changes

    def _native_light(self, light_id):
        for item in self.registry.to_dict()["lights"]:
            if item["id"] == light_id and item.get("provider") == "hue" \
                    and item["hue"]["channels"]:
                return item["hue"]["light"]
        return None

    def remove(self, light_id):
        """Remove an enrolled light, a migrated one too; its identity stays for a rejoin.

        A Hue light leaves the paired area first, so the relay never meets an
        area member that is not enrolled; a refused removal puts it back."""
        with self.changes:
            self._remove(light_id)

    def _remove(self, light_id):
        with self.lock:
            self._check(light_id, LIGHT_ID)
            if light_id not in self.registry.ids:
                raise LightRefused("not_enrolled")
            # A light for scenes only is not in the area.
            native = self._native_light(light_id)
        before = None
        if native is not None:
            if self.area is None:
                raise LightRefused("unavailable")
            before = self._area_call(self.area.leave, native)
        try:
            with self.lock:
                self._check(light_id, LIGHT_ID)
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
