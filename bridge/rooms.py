"""Rooms of connected lights: the room index and one desk layout per room (#78).

``rooms.json`` lists the rooms in order, the room of each connected light and the
room of the Sync Box (the Sync room). Each room's layout is ``rooms/<id>.json`` in
the version seven shape of ``desk_layout``; its ``room`` field is the room's name.

Before the first write the index does not exist: the single desk layout
(``desk-layout.json``) is read as the first room, which holds every light and the
Sync Box. The first write creates the room files and removes the old layout.

``Rooms`` answers the per-light questions of ``LayoutStore`` across rooms: a light's
corrections, names, calibration and scene fade come from its room; Sync membership
and the Sync profile come from the Sync room only.
"""

import copy
import json
import os
import re
import secrets
import tempfile
import threading
from pathlib import Path

from desk_layout import (
    LAYOUT_VERSION,
    MIGRATED_DESK,
    ROLES,
    LayoutStore,
    calibration_values,
    validate_layout,
)

ROOMS_VERSION = 1
INDEX_FILE = "rooms.json"
LAYOUTS_DIR = "rooms"
LEGACY_LAYOUT = "desk-layout.json"
# The room that the single desk layout becomes; every process derives the same id.
FIRST_ROOM = "room-00000001"
MAX_ROOMS = 10
# Before the first write: the single layout is the first room, with Sync.
SINGLE = {"version": ROOMS_VERSION, "rooms": [FIRST_ROOM], "lights": {}, "sync": FIRST_ROOM,
          "default": FIRST_ROOM}
# A corrupt index without a good one: no permission to drive Sync.
UNREADABLE = {"version": ROOMS_VERSION, "rooms": [FIRST_ROOM], "lights": {}, "sync": None,
              "default": FIRST_ROOM}
ROOM_ID = re.compile(r"room-[0-9a-f]{8}")
DEVICE_ID = re.compile(r"device-[a-f0-9]{32}")
MAX_ROWS = 12


class RoomRefused(ValueError):
    """A change of the rooms that is not allowed now; ``reason`` names why."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def room_name(value):
    """A room name of 1-40 characters, without surrounding spaces."""
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 40:
        raise RoomRefused("invalid_name")
    return value.strip()


def new_layout(name):
    """The layout of a new room: the default desk and monitor of Light Studio, no rows."""
    return {"version": LAYOUT_VERSION, "gridStepCm": 5, "room": name,
            # The desk centre is x 100, as in Light Studio: the screen stands in the middle.
            "furniture": [{"id": MIGRATED_DESK, "kind": "desk", "x": 100,
                           "width": 160, "height": 85}],
            "screen": {"kind": "monitor", "positionCm": {"x": 100, "height": 110},
                       "inches": 32, "on": MIGRATED_DESK},
            "devices": []}


def _light(value):
    return isinstance(value, str) and (value in ROLES or DEVICE_ID.fullmatch(value) is not None)


def validate_index(value):
    """Return the canonical room index or raise ValueError."""
    # ``default`` is the room of a light without a recorded room. An index written
    # before it existed has none: such lights belong to the original room while it is
    # listed (only the room-order bug put another room first), otherwise to the first.
    keys = {"version", "rooms", "lights", "sync"}
    if not isinstance(value, dict) or set(value) not in (keys, keys | {"default"}):
        raise ValueError("Expected a room index")
    if value["version"] != ROOMS_VERSION or type(value["version"]) is not int:
        raise ValueError("Unknown room index version")
    rooms = value["rooms"]
    if (not isinstance(rooms, list) or not 1 <= len(rooms) <= MAX_ROOMS
            or any(not isinstance(room, str) or not ROOM_ID.fullmatch(room) for room in rooms)
            or len(set(rooms)) != len(rooms)):
        raise ValueError("Expected one to ten distinct rooms")
    lights = value["lights"]
    if (not isinstance(lights, dict) or any(not _light(light) for light in lights)
            or any(room not in rooms for room in lights.values())):
        raise ValueError("Every light must be in a listed room")
    if value["sync"] is not None and value["sync"] not in rooms:
        raise ValueError("The Sync Box must be in a listed room")
    default = value.get("default", FIRST_ROOM if FIRST_ROOM in rooms else rooms[0])
    if default not in rooms:
        raise ValueError("The default room must be listed")
    return {"version": ROOMS_VERSION, "rooms": list(rooms), "lights": dict(lights),
            "sync": value["sync"], "default": default}


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", prefix="tmp_rooms_", dir=path.parent,
                                         delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class Rooms:
    def __init__(self, state_dir, native=None, migrated=None):
        self.state_dir = Path(state_dir)
        self.index_path = self.state_dir / INDEX_FILE
        # Passed to every room's store; they only shape the Sync profile.
        self.native, self.migrated = native, migrated
        self.lock = threading.RLock()
        self.signature = None
        self.data = None
        self.stores = {}
        self.fade_cache = {}

    # -- index ----------------------------------------------------------------
    def index(self):
        """A copy of the room index (see ``_index``)."""
        return copy.deepcopy(self._index())

    def _index(self):
        """The room index, shared and read-only.

        Before the first write, the single layout is the first room.

        An unreadable index keeps the last good one. Without one, a corrupt index
        is no permission to drive Sync: it lists the first room without the Sync Box.
        """
        with self.lock:
            try:
                stat = self.index_path.stat()
            except FileNotFoundError:
                self.signature, self.data = None, None
                return SINGLE
            except OSError:
                stat = None
            if stat is not None:
                signature = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
                if signature != self.signature:
                    try:
                        self.data = validate_index(json.loads(self.index_path.read_text()))
                        self.signature = signature
                    except (OSError, ValueError, TypeError):
                        pass
            if self.data is None:
                return UNREADABLE
            return self.data

    def materialized(self):
        return self.index_path.exists()

    def room_of(self, light, index=None):
        """The light's room: its recorded room, otherwise the default room."""
        index = index or self._index()
        return index["lights"].get(light, index["default"])

    def sync_room(self):
        return self._index()["sync"]

    # -- layouts --------------------------------------------------------------
    def layout_path(self, room):
        if not self.materialized():
            return self.state_dir / LEGACY_LAYOUT if room == FIRST_ROOM else None
        return self.state_dir / LAYOUTS_DIR / (room + ".json")

    def store(self, room):
        """The layout store of ``room``, or None for a room that is not listed."""
        with self.lock:
            if room not in self._index()["rooms"]:
                return None
            path = self.layout_path(room)
            store = self.stores.get(path)
            if store is None:
                store = self.stores[path] = LayoutStore(path, self.native, self.migrated)
            return store

    def save(self, room, value):
        """Save ``room``'s layout; the first write creates the room files."""
        with self.lock:
            index = self._index()
            if room not in index["rooms"]:
                raise ValueError("Unknown room")
            # A light's row belongs to the light's room only.
            rows = value.get("devices") if isinstance(value, dict) else None
            if any(isinstance(row, dict) and _light(row.get("id"))
                   and self.room_of(row["id"], index) != room
                   for row in (rows if isinstance(rows, list) else ())):
                raise ValueError("A light's row belongs to its own room")
            # The Sync Box's room keeps a screen: Sync samples the picture on it.
            if room == index["sync"] and "screen" not in validate_layout(value):
                raise RoomRefused("no_screen")
            if self.materialized():
                return self.store(room).save(value)
            result = LayoutStore(self.state_dir / LAYOUTS_DIR / (room + ".json"),
                                 self.native, self.migrated).save(value)
            self.write_index(index)
            (self.state_dir / LEGACY_LAYOUT).unlink(missing_ok=True)
            return result

    def write_index(self, value):
        data = validate_index(value)
        with self.lock:
            _write_json(self.index_path, data)
            self.signature = None
            self._index()

    # -- LayoutStore questions across rooms -----------------------------------
    def _own_layout(self, room, index):
        """``room``'s layout without the rows of lights that belong to another room."""
        store = self.store(room)
        data = store.snapshot() if store is not None else None
        if data is not None:
            data["devices"] = [row for row in data["devices"] if not _light(row["id"])
                               or self.room_of(row["id"], index) == room]
        return data

    def _room_store(self, light):
        return self.store(self.room_of(light))

    def _sync_store(self, light):
        """The store of the Sync room when ``light`` is in it, otherwise None."""
        index = self._index()
        sync = index["sync"]
        if sync is None or self.room_of(light, index) != sync:
            return None
        return self.store(sync)

    def scale(self, key, fallback):
        store = self._room_store(key)
        return store.scale(key, fallback) if store is not None else fallback

    def scene_scale(self, key, fallback):
        store = self._room_store(key)
        return store.scene_scale(key, fallback) if store is not None else fallback

    def calibration(self, light):
        store = self._room_store(light)
        return store.calibration(light) if store is not None else calibration_values({})

    def names(self):
        result = {}
        index = self._index()
        for room in index["rooms"]:
            store = self.store(room)
            for light, name in (store.names() if store is not None else {}).items():
                if self.room_of(light, index) == room:
                    result[light] = name
        return result

    def present(self, role, fallback=True):
        """Scene membership, from the light's own room."""
        store = self._room_store(role)
        return store.present(role, fallback) if store is not None else fallback

    def enabled(self, role, fallback=True):
        """Sync membership: only a light of the Sync room can take part."""
        store = self._sync_store(role)
        return store.enabled(role, fallback) if store is not None else False

    def sync_off_roles(self):
        sync = self.sync_room()
        store = self.store(sync) if sync is not None else None
        if store is None:
            return []
        index = self._index()
        return [role for role in store.sync_off_roles() if self.room_of(role, index) == sync]

    def scene_fade(self):
        """Every room's scene fade gains for its own lights, or None without any layout."""
        from sync_projection import scene_fade

        with self.lock:
            index = self._index()
            result, found = {}, False
            for room in index["rooms"]:
                data = self._own_layout(room, index)
                if data is None:
                    continue
                found = True
                key = (self.signature, json.dumps(data, sort_keys=True))
                cached = self.fade_cache.get(room)
                if cached is None or cached[0] != key:
                    cached = self.fade_cache[room] = (key, scene_fade(data))
                result.update(copy.deepcopy(cached[1]))
            return result if found else None

    def sync_profile(self):
        """The Sync profile of the Sync room's own lights; None without a Sync room
        or its layout."""
        from sync_projection import sync_profile

        index = self._index()
        sync = index["sync"]
        data = self._own_layout(sync, index) if sync is not None else None
        return sync_profile(data, self.native, self.migrated) if data is not None else None

    def layout_for(self, light):
        """The layout of the light's room, or None when it has none."""
        store = self._room_store(light)
        return store.snapshot() if store is not None else None

    # -- room management (Light Studio, #78 stage 4) ----------------------------
    def _materialize(self):
        """Create the room files from the single layout before the rooms change."""
        if self.materialized():
            return
        legacy = self.store(FIRST_ROOM).snapshot()
        if legacy is not None:
            self.save(FIRST_ROOM, legacy)
        else:
            self.write_index(SINGLE)

    def _layout_store(self, room):
        return LayoutStore(self.state_dir / LAYOUTS_DIR / (room + ".json"),
                           self.native, self.migrated)

    def _known(self, room, index):
        if room not in index["rooms"]:
            raise RoomRefused("unknown_room")

    def state(self):
        """A copy of the index and every room's layout, for ``restore``."""
        with self.lock:
            index = self.index()
            layouts = {}
            for room in index["rooms"]:
                store = self.store(room)
                layouts[room] = store.snapshot() if store is not None else None
            return {"materialized": self.materialized(), "index": index,
                    "layouts": layouts}

    def restore(self, state):
        """Put back the rooms that ``state`` recorded (after a refused change)."""
        with self.lock:
            if state["materialized"]:
                self._restore_rooms(state)
            else:
                self._restore_single(state)

    def _restore_rooms(self, state):
        for room in self.index()["rooms"]:
            if room not in state["index"]["rooms"]:
                (self.state_dir / LAYOUTS_DIR / (room + ".json")).unlink(missing_ok=True)
        self.write_index(state["index"])
        for room, data in state["layouts"].items():
            path = self.state_dir / LAYOUTS_DIR / (room + ".json")
            if data is not None:
                self._layout_store(room).save(data)
            else:
                # A room without a layout before the change has none again.
                path.unlink(missing_ok=True)
                self.stores.pop(path, None)

    def _restore_single(self, state):
        """Back to the single layout of a state captured before the rooms existed: the
        captured layout in ``desk-layout.json``, no index and no room files."""
        legacy = state["layouts"].get(FIRST_ROOM)
        if legacy is not None:
            LayoutStore(self.state_dir / LEGACY_LAYOUT).save(legacy)
        for room in self.index()["rooms"]:
            (self.state_dir / LAYOUTS_DIR / (room + ".json")).unlink(missing_ok=True)
        self.index_path.unlink(missing_ok=True)
        self.signature, self.data = None, None

    def create(self, name):
        """Add a room with the default layout at the end; return its id."""
        name = room_name(name)
        with self.lock:
            self._materialize()
            index = self.index()
            if len(index["rooms"]) >= MAX_ROOMS:
                raise RoomRefused("room_limit")
            room = FIRST_ROOM
            while room in index["rooms"] or room == FIRST_ROOM:
                room = "room-" + secrets.token_hex(4)
            self._layout_store(room).save(new_layout(name))
            index["rooms"].append(room)
            self.write_index(index)
            return room

    def rename(self, room, name):
        name = room_name(name)
        with self.lock:
            index = self.index()
            self._known(room, index)
            data = self._own_layout(room, index) or new_layout(name)
            data["room"] = name
            return self.save(room, data)

    def reorder(self, order):
        """Put the rooms in ``order``; no light changes its room, since the default room
        is kept apart from the order."""
        with self.lock:
            self._materialize()
            index = self.index()
            if (not isinstance(order, list) or len(order) != len(index["rooms"])
                    or set(order) != set(index["rooms"])):
                raise RoomRefused("unknown_room")
            index["rooms"] = list(order)
            self.write_index(index)

    def delete(self, room, connected):
        """Delete an empty room: no connected light (``connected`` lists them all) and
        no Sync Box. Its layout goes with it, with the rows of drawings and of lights
        that are not connected."""
        with self.lock:
            self._materialize()
            index = self.index()
            self._known(room, index)
            if len(index["rooms"]) == 1:
                raise RoomRefused("last_room")
            if index["sync"] == room:
                raise RoomRefused("has_sync_box")
            if any(self.room_of(light, index) == room for light in connected):
                raise RoomRefused("has_lights")
            index["rooms"].remove(room)
            index["lights"] = {light: home for light, home in index["lights"].items()
                               if home != room}
            if index["default"] == room:
                # It held no connected light; a light without a room goes first now.
                index["default"] = index["rooms"][0]
            self.write_index(index)
            (self.state_dir / LAYOUTS_DIR / (room + ".json")).unlink(missing_ok=True)
            self.stores.pop(self.state_dir / LAYOUTS_DIR / (room + ".json"), None)
            self.fade_cache.pop(room, None)

    def move(self, light, room):
        """Put ``light`` in ``room``; its layout row moves with its corrections."""
        if not _light(light):
            raise RoomRefused("unknown_light")
        with self.lock:
            self._materialize()
            index = self.index()
            self._known(room, index)
            old = self.room_of(light, index)
            if old == room:
                return
            source = self._own_layout(old, index)
            target = self._own_layout(room, index) or new_layout("Room")
            row = next((row for row in (source or {}).get("devices", ())
                        if row["id"] == light), None)
            if row is not None and len(target["devices"]) >= MAX_ROWS:
                raise RoomRefused("layout_full")
            index["lights"][light] = room
            self.write_index(index)
            if row is not None:
                target["devices"] = [item for item in target["devices"]
                                     if item["id"] != light] + [row]
                self.save(room, target)
                source["devices"] = [item for item in source["devices"]
                                     if item["id"] != light]
                self.save(old, source)

    def place(self, light, room):
        """Record the room of a light that is connected now (before its enrollment)."""
        if not _light(light):
            raise RoomRefused("unknown_light")
        with self.lock:
            self._materialize()
            index = self.index()
            self._known(room, index)
            index["lights"][light] = room
            self.write_index(index)

    def place_sync_box(self, room):
        """Register the Sync Box in ``room``, or in no room with None."""
        with self.lock:
            self._materialize()
            index = self.index()
            if room is not None:
                self._known(room, index)
                # A room without a saved layout has no screen either.
                layout = self.store(room).snapshot()
                if layout is None or "screen" not in layout:
                    raise RoomRefused("no_screen")
            index["sync"] = room
            self.write_index(index)
