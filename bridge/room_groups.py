"""One Room group of the virtual bridge per room (#78 stage 3).

Each room of the room index is a Room group on the virtual bridge, named as the
room, with the virtual lights of its connected lights. The first room keeps the
existing group "1". A new room's group takes the lowest free group id and an
``id_v2`` derived from the room id, so it stays the same across restarts.
``room-groups.json`` in the bridge state records which group belongs to which
room: only these groups are changed or deleted, never a group that the owner
created in the Hue app.

A light is in the group of its room only, as on a Hue bridge. A scene of a room's
group never drives a light of another room: a light that left the group is
dropped from the scene's stored light states. Deleting a room deletes its group
and the group's scenes. A virtual light that no connected light uses (a removed
light) stays where it is.
"""

import json
import os
import re
import tempfile
import uuid
from pathlib import Path

GROUPS_FILE = "room-groups.json"
FIRST_GROUP = "1"
# The Hue Room class of a room created after the first one.
NEW_ROOM_CLASS = "Other"
_ROOM_ID = re.compile(r"room-[0-9a-f]{8}\Z")
_GROUP_ID = re.compile(r"[1-9][0-9]{0,3}\Z")


def group_uuid(namespace, room):
    """The stable ``id_v2`` of a room's group."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{namespace}/{room}"))


def load_owned(state, first_room):
    """The recorded ``{room: group id}``; the first room always owns group "1"."""
    owned = {first_room: FIRST_GROUP}
    try:
        data = json.loads((Path(state) / GROUPS_FILE).read_text())
    except (OSError, ValueError):
        return owned
    if isinstance(data, dict):
        for room, group in data.items():
            if (isinstance(room, str) and _ROOM_ID.match(room) and isinstance(group, str)
                    and _GROUP_ID.match(group) and group not in owned.values()
                    and room not in owned):
                owned[room] = group
    return owned


def save_owned(state, owned):
    path = Path(state) / GROUPS_FILE
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix="tmp_room_groups_",
                                         delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(owned, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def plan(rooms, owned, taken):
    """Return ``(groups, deleted)``: ``{room: group id}`` for every listed room, and
    the ids of the owned groups whose room is gone.

    ``rooms`` lists the room ids in order; ``owned`` is the recorded mapping;
    ``taken`` holds every group id on the bridge. A room without a group takes the
    lowest free id from 2 up.
    """
    groups, deleted = {}, []
    for room, group in owned.items():
        if room in rooms:
            groups[room] = group
        else:
            deleted.append(group)
    used = set(taken) | set(groups.values()) | {"0"}
    for room in rooms:
        if room not in groups:
            # Group "1" belongs to the first room only, even after that room is gone.
            group = next(str(n) for n in range(2, 10000) if str(n) not in used)
            groups[room] = group
            used.add(group)
    # A deleted room's group id is free again only after the group is gone.
    return {room: groups[room] for room in rooms}, sorted(deleted, key=int)


def members(rooms, room_of, slots):
    """Return ``{room: [slot, ...]}`` of the virtual lights of connected lights.

    ``slots`` maps each virtual light slot of a connected light to its registry id.
    """
    result = {room: [] for room in rooms}
    for slot, light in sorted(slots.items(), key=lambda item: int(item[0])):
        room = room_of(light)
        if room in result:
            result[room].append(slot)
    return result


def _scene_group(scene):
    reference = getattr(scene, "group", None)
    return reference() if callable(reference) else None


def apply(config, group_class, namespace, groups, deleted, wanted, names):
    """Bring the bridge's groups and scenes in line with the rooms.

    ``config`` is the diyHue configuration; ``group_class`` creates a group;
    ``groups`` maps each room to its group id; ``deleted`` lists the groups of
    gone rooms; ``wanted`` maps each room to its slots; ``names`` maps each room to
    its name. Return True when anything changed.
    """
    changed = False
    for group_id in deleted:
        group = config["groups"].get(group_id)
        if group is None:
            continue
        for scene_id, scene in list(config["scenes"].items()):
            if _scene_group(scene) is group:
                del config["scenes"][scene_id]
        del config["groups"][group_id]
        changed = True
    managed = {slot for slots in wanted.values() for slot in slots}
    for room, group_id in groups.items():
        # A room without a layout name keeps its group's name.
        name = names.get(room)
        group = config["groups"].get(group_id)
        if group is None:
            group = group_class({"id_v1": group_id, "id_v2": group_uuid(namespace, room),
                                 "name": name or "Room", "type": "Room",
                                 "class": NEW_ROOM_CLASS})
            config["groups"][group_id] = group
            changed = True
        if name and group.name != name:
            group.name = name
            changed = True
        mine = set(wanted.get(room, ()))
        # The scenes of the group share its light list, so it changes in place.
        kept = [reference for reference in group.lights if reference() is not None
                and (reference().id_v1 not in managed or reference().id_v1 in mine)]
        if len(kept) != len(group.lights):
            group.lights[:] = kept
            changed = True
        present = {reference().id_v1 for reference in group.lights}
        for slot in wanted.get(room, ()):
            if slot not in present and slot in config["lights"]:
                group.add_light(config["lights"][slot])
                changed = True
        for scene in config["scenes"].values():
            if _scene_group(scene) is not group:
                continue
            for light in list(scene.lightstates.keys()):
                if light.id_v1 in managed and light.id_v1 not in mine:
                    del scene.lightstates[light]
                    changed = True
    return changed


def rename(config, groups, names):
    """Give each room's group the room's name; return True when one changed."""
    changed = False
    for room, group_id in groups.items():
        group = config["groups"].get(group_id)
        name = names.get(room)
        if group is not None and name and group.name != name:
            group.name = name
            changed = True
    return changed


def room_names(rooms):
    """``{room: name}`` from each room's layout (its ``room`` field)."""
    result = {}
    for room in rooms.index()["rooms"]:
        store = rooms.store(room)
        data = store.snapshot() if store is not None else None
        name = data.get("room") if isinstance(data, dict) else None
        if isinstance(name, str) and name.strip():
            result[room] = name.strip()[:32]
    return result


def signature(rooms):
    """The room set and the recorded rooms of the lights; a change restarts the bridge."""
    index = rooms.index()
    return json.dumps([index["rooms"], index["lights"]], sort_keys=True)
