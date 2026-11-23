"""Owner enrollment and removal of discovered Govee lights in the light registry.

The adapter is the only writer of the registry: it also records a lamp's new
address there. An enrollment or a removal is checked against the discovery
catalog at that moment (discovered, online, unambiguous, not a migrated lamp),
saved atomically, and then the adapter restarts so that every layer starts from
the new light set. Docker restarts the finished container. While Sync output is
active the light set is not changed.
"""

import threading

from lights_can_communicate_v2_core.light_registry import DEVICE_ID, RegistryError

# Reasons a valid request is refused (HTTP 409); Light Studio shows only these.
REASONS = ("sync_active", "restarting", "enrolled", "not_enrolled", "unavailable")


class LightRefused(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class LightEnrollment:
    def __init__(self, path, registry, catalog, busy, restart):
        self.path = path
        self.registry = registry
        self.catalog = catalog
        # True while Sync output owns the lamps.
        self.busy = busy
        # Called once after a changed registry is saved.
        self.restart = restart
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
        """Add a discovered Govee light; raise LightRefused or ValueError."""
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

    def remove(self, light_id):
        """Remove an enrolled discovered Govee light; its identity stays for a rejoin."""
        with self.lock:
            self._check(light_id)
            if light_id not in self.registry.ids:
                raise LightRefused("not_enrolled")
            self._apply(self.registry.without_govee_light(light_id))
        self.restart()

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
