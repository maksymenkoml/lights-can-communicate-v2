"""Switch lamps with ``syncRole: "off"`` off for each Sync session.

A session starts when ``VideoOutput`` becomes active and ends when it is
released (explicit stop or grace elapsed). At the start each "off" lamp's power
is recorded and the lamp is switched off; at the end the recorded On is
restored. A lamp that was already off stays off, and an owner power change
during the session wins: the lamp is then not restored.

- Govee: a Sync hold in the power authority (``HomeKitPower`` or
  ``PowerOwner``). The hold is not owner intent and closes the color writer.
  Without a power authority the adapter has no power path, so Govee "off" lamps
  are left untouched (they keep their own light, like "own").
- Native Hue: ``HueStripProxy.set_power``. The role is outside the composition,
  so the ``VideoOutput.safe()`` fence never requires it to be on.

The roles switched off are persisted before any command (atomic, 0600). On
adapter start a leftover file is restored and deleted while no Sync runs, so a
crash never leaves a lamp off. Changing a lamp's ``syncRole`` while a session
runs applies at once: a lamp set to "off" is switched off, and a lamp that
leaves "off" gets its recorded power back. Each lamp is switched off at most
once per session, so an owner who switches it on again keeps it on.
"""

import json
import math
import os
import threading
import time

from lights_can_communicate_v2_core.light_registry import GOVEE_ID, LEGACY_SIDES

# A role is a Govee light id or the role name of a native Hue light.
NATIVE_ROLES = ("strip", "hue_left", "hue_center", "hue_right")
STATE_FILE = "sync-off-session.json"
STATE_VERSION = 1
LEGACY_ROLES = {side: light for light, side in LEGACY_SIDES.items()}
RETRY_SECONDS = 1.0
# After a restart, wait this long for a native Govee reading before restoring.
OBSERVE_WAIT_SECONDS = 15.0


def native_power(proxy):
    """Return a native Hue light's fresh power (True/False), or None when unknown."""
    try:
        state = proxy.snapshot()
    except (OSError, ValueError, KeyError, TypeError):
        return None
    age = state.get("native_age")
    if (state.get("healthy") is not True or type(age) not in (int, float)
            or not math.isfinite(age) or not 0 <= age <= 2
            or type(state.get("native_on")) is not bool):
        return None
    return state["native_on"]


class SyncOffSession:
    def __init__(self, layout_store, active, state_file, govee=None, native=None, observe=None,
                 clock=time.monotonic):
        self.layout_store, self.active = layout_store, active
        self.state_file = str(state_file)
        self.govee, self.native = govee, dict(native or {})
        self.observe, self.clock = observe, clock
        self.lock = threading.RLock()
        self.running = False
        self.closed = False
        self.started = clock()
        self.next_retry = float("-inf")
        # role -> True: switched off by this adapter, restore On at the end.
        self.migrated = False
        self.records = self._load()
        # Native roles whose Off was confirmed; a later fresh On is an owner change.
        self.confirmed_off = set()
        # Roles this session has already handled (switched off, or found off or
        # unknown); they are not switched off again before the next session.
        self.applied = set()
        # Role -> earliest retry of a failed restore for a lamp that left Off.
        self.restore_retry = {}
        self.last_result = "idle"
        if self.migrated:
            # A file from before the light ids is converted once and rewritten.
            self._persist()
        for role in list(self.records):
            if role not in NATIVE_ROLES and self.govee is not None:
                # Keep tick() from adopting the crash-time Off before it is restored.
                try:
                    self.govee.sync_hold(role, send=False)
                except ValueError:
                    # The light is no longer driven; there is nothing to restore.
                    del self.records[role]

    # Persistence ----------------------------------------------------------

    def _load(self):
        try:
            with open(self.state_file, "rb") as stream:
                data = json.loads(stream.read(4096))
            roles = data.get("roles") if data.get("version") == STATE_VERSION else None
            if not isinstance(roles, list):
                return {}
            records = {}
            for role in roles:
                if role in LEGACY_ROLES:
                    role = LEGACY_ROLES[role]
                    self.migrated = True
                if role in NATIVE_ROLES or (type(role) is str and GOVEE_ID.match(role)):
                    records[role] = True
            return records
        except (OSError, ValueError, TypeError, AttributeError):
            return {}

    def _save(self):
        if not self.records:
            try:
                os.unlink(self.state_file)
            except FileNotFoundError:
                pass
            return
        data = (json.dumps({"version": STATE_VERSION, "roles": sorted(self.records)})
                + "\n").encode()
        temporary = f"{self.state_file}.{os.getpid()}.tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.state_file)
        except OSError:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    def _persist(self):
        """Save the records; False when the recovery file could not be written."""
        try:
            self._save()
        except OSError:
            self.last_result = "state file unavailable"
            return False
        return True

    # Session --------------------------------------------------------------

    def roles(self):
        loader = getattr(self.layout_store, "sync_off_roles", None)
        return loader() if loader is not None else []

    def start(self, roles=None):
        for role in self.roles() if roles is None else roles:
            self.applied.add(role)
            if role in self.records:
                continue
            if role not in NATIVE_ROLES:
                if self.govee is None:
                    continue
                # Persist first: a crash during the switch must still restore, so
                # a lamp is left untouched when its record cannot be saved.
                self.records[role] = True
                if not self._persist():
                    del self.records[role]
                    continue
                try:
                    held = self.govee.sync_hold(role)
                except (OSError, ValueError):
                    held = self.govee.sync_held(role)
                if not held:
                    del self.records[role]
                    self._persist()
            elif role in self.native:
                proxy = self.native[role]
                if native_power(proxy) is not True:
                    # Already off, or unknown: an unknown lamp is never switched.
                    continue
                self.records[role] = True
                if not self._persist():
                    del self.records[role]
                    continue
                try:
                    if proxy.set_power(False):
                        self.confirmed_off.add(role)
                except (OSError, ValueError, KeyError):
                    pass
        self.last_result = "session"

    def end(self, roles=None):
        """Restore recorded lamps; failures stay recorded and are retried."""
        for role in [role for role in self.records if roles is None or role in roles]:
            if role not in NATIVE_ROLES:
                if self.govee is None:
                    del self.records[role]
                    continue
                if (self.observe is not None and self.clock() - self.started
                        < OBSERVE_WAIT_SECONDS
                        and self.observe().get(role, {}).get("on") is None):
                    continue  # After a restart: wait for the first native reading.
                try:
                    result = self.govee.sync_release(role)
                except (OSError, ValueError):
                    continue
                if result == "failed":
                    continue  # Still off: keep the record and retry.
                del self.records[role]
            else:
                proxy = self.native.get(role)
                try:
                    if proxy is None or proxy.set_power(True):
                        del self.records[role]
                except (OSError, ValueError, KeyError):
                    pass
            self.confirmed_off.discard(role)
        self._persist()
        if roles is None:
            self.last_result = "idle" if not self.records else "restore pending"

    def owner_power(self, role):
        """The owner switched this lamp during the session: do not restore it."""
        with self.lock:
            if role not in NATIVE_ROLES and self.govee is not None:
                self.govee.owner_power(role)
            if role in self.records:
                del self.records[role]
                self.confirmed_off.discard(role)
                self._persist()

    def _watch_owner(self):
        changed = False
        for role in list(self.records):
            if role not in NATIVE_ROLES:
                owner = self.govee is not None and not self.govee.sync_held(role)
            else:
                # A fresh On after the confirmed Off came from the owner (e.g. Hue app).
                owner = (role in self.confirmed_off
                         and native_power(self.native[role]) is True)
            if owner:
                del self.records[role]
                self.confirmed_off.discard(role)
                changed = True
        if changed:
            self._persist()

    def _follow_layout(self):
        """Apply syncRole changes made while the session runs."""
        current = self.roles()
        # A lamp that leaves "off" may be set to "off" again later in the session.
        self.applied.intersection_update(current)
        added = [role for role in current if role not in self.applied]
        if added:
            self.start(added)
        now = self.clock()
        # A failed restore waits for its own retry time; other lamps leaving Off
        # are restored at once.
        leaving = [role for role in self.records if role not in current
                   and self.restore_retry.get(role, float("-inf")) <= now]
        if leaving:
            self.end(leaving)
            for role in leaving:
                if role in self.records:
                    self.restore_retry[role] = now + RETRY_SECONDS
        for role in list(self.restore_retry):
            if role not in self.records or role in current:
                del self.restore_retry[role]

    def step(self):
        with self.lock:
            if self.closed:
                return
            active = bool(self.active())
            if active and not self.running:
                self.running = True
                self.applied = set()
                self.start()
            elif not active and self.running:
                self.running = False
                self.applied = set()
                self.end()
                self.next_retry = self.clock() + RETRY_SECONDS
            elif not active and self.records and self.clock() >= self.next_retry:
                self.end()
                self.next_retry = self.clock() + RETRY_SECONDS
            elif self.running:
                self._watch_owner()
                self._follow_layout()

    def close(self):
        """Adapter shutdown: restore now; anything not restored stays on disk."""
        with self.lock:
            if self.closed:
                return
            self.closed = True
            self.running = False
            self.started = float("-inf")
            if self.records:
                self.end()

    def run(self, stop):
        while not stop.is_set():
            try:
                self.step()
            except Exception:
                # A failed lamp must not stop the session watcher; it retries.
                self.last_result = "error"
            stop.wait(0.1)

    def snapshot(self):
        with self.lock:
            return {"running": self.running, "roles": sorted(self.records),
                    "last_result": self.last_result}
