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

from desk_layout import ROLES, LayoutStore, calibration_values

ROOMS_VERSION = 1
INDEX_FILE = "rooms.json"
LAYOUTS_DIR = "rooms"
LEGACY_LAYOUT = "desk-layout.json"
# The room that the single desk layout becomes; every process derives the same id.
FIRST_ROOM = "room-00000001"
MAX_ROOMS = 10
# Before the first write: the single layout is the first room, with Sync.
SINGLE = {"version": ROOMS_VERSION, "rooms": [FIRST_ROOM], "lights": {}, "sync": FIRST_ROOM}
# A corrupt index without a good one: no permission to drive Sync.
UNREADABLE = {"version": ROOMS_VERSION, "rooms": [FIRST_ROOM], "lights": {}, "sync": None}
ROOM_ID = re.compile(r"room-[0-9a-f]{8}")
DEVICE_ID = re.compile(r"device-[a-f0-9]{32}")


def room_id():
    return "room-" + secrets.token_hex(4)


def _light(value):
    return isinstance(value, str) and (value in ROLES or DEVICE_ID.fullmatch(value) is not None)


def validate_index(value):
    """Return the canonical room index or raise ValueError."""
    if not isinstance(value, dict) or set(value) != {"version", "rooms", "lights", "sync"}:
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
    return {"version": ROOMS_VERSION, "rooms": list(rooms), "lights": dict(lights),
            "sync": value["sync"]}


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
        """The light's room: its recorded room, otherwise the first room."""
        index = index or self._index()
        return index["lights"].get(light, index["rooms"][0])

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
            if any(isinstance(row, dict) and index["lights"].get(row.get("id"), room) != room
                   for row in (rows if isinstance(rows, list) else ())):
                raise ValueError("A light's row belongs to its own room")
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
        result, found = {}, False
        index = self._index()
        for room in index["rooms"]:
            store = self.store(room)
            fade = store.scene_fade() if store is not None else None
            if fade is None:
                continue
            found = True
            result.update({light: gains for light, gains in fade.items()
                           if self.room_of(light, index) == room})
        return result if found else None

    def sync_profile(self):
        """The Sync profile of the Sync room; None without a Sync room or its layout."""
        sync = self.sync_room()
        store = self.store(sync) if sync is not None else None
        return store.sync_profile() if store is not None else None

    def layout_for(self, light):
        """The layout of the light's room, or None when it has none."""
        store = self._room_store(light)
        return store.snapshot() if store is not None else None
