"""Rooms of connected lights: the room index and one layout per room (#78 stage 1)."""

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from desk_layout import LayoutStore, validate_layout  # noqa: E402
from rooms import FIRST_ROOM, RoomRefused, Rooms, validate_index  # noqa: E402
from test_desk_layout import layout  # noqa: E402

SECOND = "room-0a1b2c3d"
ROLES = ("govee-left", "govee-right", "hue-monitor", "play-left", "play-center", "play-right")


def current(name="Office", rows=None, **changes):
    """A version seven layout with the six migrated rows (or only ``rows``)."""
    data = validate_layout(layout())
    data["room"] = name
    if rows is not None:
        data["devices"] = [row for row in data["devices"] if row["id"] in rows]
    for row in data["devices"]:
        row.update(changes.get(row["id"], {}))
    return data


class RoomsTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory(prefix="tmp_rooms_")
        self.addCleanup(temp.cleanup)
        self.state = Path(temp.name)

    def two_rooms(self, sync=FIRST_ROOM):
        """Office holds every light except play-right, which is in the bedroom."""
        (self.state / "rooms").mkdir(exist_ok=True)
        office = current("Office", rows=[role for role in ROLES if role != "play-right"])
        bedroom = current("Bedroom", rows=["play-right"],
                          **{"play-right": {"scale": 40, "sceneScale": 30, "name": "Bed"}})
        (self.state / "rooms" / (FIRST_ROOM + ".json")).write_text(json.dumps(office))
        (self.state / "rooms" / (SECOND + ".json")).write_text(json.dumps(bedroom))
        (self.state / "rooms.json").write_text(json.dumps({
            "version": 1, "rooms": [FIRST_ROOM, SECOND],
            "lights": {"play-right": SECOND}, "sync": sync}))
        return Rooms(self.state)

    def test_the_single_layout_is_the_first_room_with_every_light_and_sync(self):
        data = current(**{"govee-left": {"sceneScale": 40, "name": "Left"}})
        (self.state / "desk-layout.json").write_text(json.dumps(data))
        rooms, single = Rooms(self.state), LayoutStore(self.state / "desk-layout.json")
        self.assertEqual(rooms.index(), {"version": 1, "rooms": [FIRST_ROOM], "lights": {},
                                         "sync": FIRST_ROOM, "default": FIRST_ROOM})
        for role in ROLES:
            self.assertEqual(rooms.scale(role, 1), single.scale(role, 1))
            self.assertEqual(rooms.scene_scale(role, 1), single.scene_scale(role, 1))
            self.assertEqual(rooms.enabled(role), single.enabled(role))
            self.assertEqual(rooms.present(role), single.present(role))
            self.assertEqual(rooms.calibration(role), single.calibration(role))
        self.assertEqual(rooms.names(), single.names())
        self.assertEqual(rooms.scene_fade(), single.scene_fade())
        self.assertEqual(rooms.sync_off_roles(), single.sync_off_roles())
        self.assertEqual(rooms.sync_profile(), single.sync_profile())
        # Nothing is written by reading.
        self.assertFalse((self.state / "rooms.json").exists())

    def test_without_any_layout_the_defaults_are_unchanged(self):
        rooms = Rooms(self.state)
        self.assertTrue(rooms.enabled("govee-left"))
        self.assertTrue(rooms.present("govee-left"))
        self.assertEqual(rooms.scale("govee-left", 0.7), 0.7)
        self.assertIsNone(rooms.sync_profile())
        self.assertIsNone(rooms.scene_fade())
        self.assertIsNone(rooms.layout_for("govee-left"))

    def test_the_first_write_creates_the_room_files_and_removes_the_single_layout(self):
        (self.state / "desk-layout.json").write_text(json.dumps(current()))
        rooms = Rooms(self.state)
        before = rooms.sync_profile()
        result = rooms.save(FIRST_ROOM, current("Office", **{"play-left": {"scale": 55}}))
        self.assertFalse((self.state / "desk-layout.json").exists())
        self.assertEqual(json.loads((self.state / "rooms" / (FIRST_ROOM + ".json")).read_text()),
                         result["layout"])
        self.assertEqual(json.loads((self.state / "rooms.json").read_text()),
                         {"version": 1, "rooms": [FIRST_ROOM], "lights": {},
                          "sync": FIRST_ROOM, "default": FIRST_ROOM})
        self.assertEqual(rooms.scale("play-left", 1), 0.55)
        self.assertEqual(before["enabled_roles"], rooms.sync_profile()["enabled_roles"])
        with self.assertRaises(ValueError):
            rooms.save(SECOND, current())

    def test_a_light_takes_its_values_from_its_own_room(self):
        rooms = self.two_rooms()
        self.assertEqual(rooms.room_of("play-right"), SECOND)
        self.assertEqual(rooms.room_of("govee-left"), FIRST_ROOM)  # not recorded: first room
        self.assertEqual(rooms.scene_scale("play-right", 1), 0.3)
        self.assertEqual(rooms.names()["play-right"], "Bed")
        self.assertTrue(rooms.present("play-right"))
        self.assertEqual(rooms.layout_for("play-right")["room"], "Bedroom")

    def test_sync_drives_only_the_lights_of_the_sync_room(self):
        rooms = self.two_rooms()
        self.assertTrue(rooms.enabled("play-left"))
        self.assertFalse(rooms.enabled("play-right"))
        self.assertNotIn("play-right", rooms.sync_profile()["enabled_roles"])
        moved = self.two_rooms(sync=SECOND)
        self.assertFalse(moved.enabled("play-left"))
        self.assertTrue(moved.enabled("play-right"))
        self.assertEqual(set(moved.sync_profile()["enabled_roles"]), {"play-right"})

    def test_without_a_sync_box_no_light_takes_part_in_sync(self):
        rooms = self.two_rooms(sync=None)
        self.assertFalse(any(rooms.enabled(role) for role in ROLES))
        self.assertIsNone(rooms.sync_profile())
        self.assertEqual(rooms.sync_off_roles(), [])
        self.assertTrue(rooms.present("play-left"))

    def test_a_row_outside_the_lights_room_is_ignored(self):
        rooms = self.two_rooms()
        stray = current("Office", **{"play-right": {"sceneScale": 90, "name": "Stray"}})
        (self.state / "rooms" / (FIRST_ROOM + ".json")).write_text(json.dumps(stray))
        self.assertEqual(rooms.scene_scale("play-right", 1), 0.3)
        self.assertEqual(rooms.names()["play-right"], "Bed")
        self.assertFalse(rooms.enabled("play-right"))
        self.assertNotIn("play-right", rooms.sync_profile()["enabled_roles"])
        self.assertNotIn("play-right", rooms.sync_profile()["lights"])

    def test_a_room_cannot_save_a_row_of_a_light_of_another_room(self):
        rooms = self.two_rooms()
        with self.assertRaises(ValueError):
            rooms.save(FIRST_ROOM, current("Office"))
        rooms.save(FIRST_ROOM, current("Office", rows=["govee-left"]))
        # A light that the index does not list is in the first room.
        with self.assertRaises(ValueError):
            rooms.save(SECOND, current("Bedroom", rows=["play-right", "govee-left"]))
        rooms.save(SECOND, current("Bedroom", rows=["play-right"]))

    def test_a_corrupt_index_keeps_the_last_good_one_and_never_starts_with_sync(self):
        rooms = self.two_rooms()
        self.assertEqual(rooms.sync_room(), FIRST_ROOM)
        (self.state / "rooms.json").write_text("{not json")
        self.assertEqual(rooms.sync_room(), FIRST_ROOM)
        fresh = Rooms(self.state)
        self.assertIsNone(fresh.sync_room())
        self.assertFalse(fresh.enabled("play-left"))

    def test_the_index_rejects_unlisted_or_duplicate_rooms(self):
        good = {"version": 1, "rooms": [FIRST_ROOM, SECOND], "lights": {"play-right": SECOND},
                "sync": FIRST_ROOM, "default": SECOND}
        self.assertEqual(validate_index(good), good)
        # An index from before the default room: its lights without a room were first.
        older = {key: value for key, value in good.items() if key != "default"}
        self.assertEqual(validate_index(older), {**older, "default": FIRST_ROOM})
        for change in ({"rooms": [FIRST_ROOM, FIRST_ROOM]}, {"rooms": []},
                       {"rooms": ["room-%08x" % n for n in range(11)]},
                       {"rooms": ["office"]}, {"lights": {"play-right": "room-ffffffff"}},
                       {"lights": {"lamp": SECOND}}, {"sync": "room-ffffffff"},
                       {"version": 2}, {"extra": 1}, {"default": "room-ffffffff"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_index({**good, **change})



class RoomManagementTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory(prefix="tmp_rooms_manage_")
        self.addCleanup(temp.cleanup)
        self.state = Path(temp.name)
        (self.state / "desk-layout.json").write_text(json.dumps(current()))
        self.rooms = Rooms(self.state)

    def refused(self, reason, call, *args):
        with self.assertRaises(RoomRefused) as refused:
            call(*args)
        self.assertEqual(refused.exception.reason, reason)

    def test_a_new_room_gets_an_id_the_default_layout_and_its_name(self):
        room = self.rooms.create("  Bedroom ")
        self.assertRegex(room, r"room-[0-9a-f]{8}\Z")
        self.assertNotEqual(room, FIRST_ROOM)
        # The single layout became the first room on the way.
        self.assertFalse((self.state / "desk-layout.json").exists())
        self.assertEqual(self.rooms.index()["rooms"], [FIRST_ROOM, room])
        layout = self.rooms.store(room).snapshot()
        self.assertEqual((layout["room"], layout["devices"], layout["screen"]["inches"]),
                         ("Bedroom", [], 32))
        self.assertEqual(self.rooms.index()["sync"], FIRST_ROOM)
        for name in ("", "   ", "x" * 41, 5):
            self.refused("invalid_name", self.rooms.create, name)
        for _ in range(8):
            self.rooms.create("Room")
        self.refused("room_limit", self.rooms.create, "Eleventh")

    def test_rename_and_reorder(self):
        room = self.rooms.create("Bedroom")
        self.rooms.rename(FIRST_ROOM, "Study")
        self.assertEqual(self.rooms.store(FIRST_ROOM).snapshot()["room"], "Study")
        self.assertEqual(len(self.rooms.store(FIRST_ROOM).snapshot()["devices"]), 6)
        self.rooms.reorder([room, FIRST_ROOM])
        self.assertEqual(self.rooms.index()["rooms"], [room, FIRST_ROOM])
        self.refused("unknown_room", self.rooms.reorder, [room])
        self.refused("unknown_room", self.rooms.rename, "room-ffffffff", "X")

    def test_a_new_order_keeps_every_light_in_its_room(self):
        room = self.rooms.create("Bedroom")
        self.rooms.move("play-right", room)
        later = "device-" + "0" * 32
        self.rooms.reorder([room, FIRST_ROOM])
        # The default room does not follow the order, so nothing is recorded.
        self.assertEqual(self.rooms.index()["lights"], {"play-right": room})
        self.assertEqual({light: self.rooms.room_of(light) for light in ROLES + (later,)},
                         {light: room if light == "play-right" else FIRST_ROOM
                          for light in ROLES + (later,)})
        self.assertIn("left", self.rooms.sync_profile()["enabled_roles"])

    def test_an_index_saved_with_another_room_first_keeps_its_lights(self):
        room = self.rooms.create("Bedroom")
        (self.state / "rooms.json").write_text(json.dumps({
            "version": 1, "rooms": [room, FIRST_ROOM], "lights": {}, "sync": FIRST_ROOM}))
        self.assertEqual(self.rooms.room_of("govee-left"), room)
        self.rooms.reorder([FIRST_ROOM, room])
        self.assertEqual(self.rooms.room_of("govee-left"), room)

    def test_deleting_the_default_room_makes_the_first_room_the_default(self):
        bedroom, hall = self.rooms.create("Bedroom"), self.rooms.create("Hall")
        for role in ROLES:
            self.rooms.move(role, hall)
        self.rooms.place_sync_box(hall)
        self.rooms.delete(FIRST_ROOM, ROLES)
        self.assertEqual(self.rooms.index()["default"], bedroom)
        self.rooms.reorder([hall, bedroom])
        self.assertEqual(self.rooms.room_of("device-" + "0" * 32), bedroom)

    def test_a_light_moves_with_its_row_and_corrections(self):
        room = self.rooms.create("Bedroom")
        before = next(row for row in self.rooms.store(FIRST_ROOM).snapshot()["devices"]
                      if row["id"] == "govee-right")
        self.rooms.move("govee-right", room)
        self.assertEqual(self.rooms.room_of("govee-right"), room)
        self.assertEqual(self.rooms.store(room).snapshot()["devices"], [before])
        self.assertNotIn("govee-right", [row["id"] for row in
                                         self.rooms.store(FIRST_ROOM).snapshot()["devices"]])
        self.assertFalse(self.rooms.enabled("govee-right"))
        # Back again, and a light without a row only changes its room.
        self.rooms.move("govee-right", FIRST_ROOM)
        self.assertIn(before, self.rooms.store(FIRST_ROOM).snapshot()["devices"])
        self.rooms.move("device-" + "a" * 32, room)
        self.assertEqual(self.rooms.room_of("device-" + "a" * 32), room)
        self.refused("unknown_light", self.rooms.move, "lamp", room)
        self.refused("unknown_room", self.rooms.move, "govee-right", "room-ffffffff")

    def test_only_an_empty_room_without_the_sync_box_can_go(self):
        room = self.rooms.create("Bedroom")
        self.rooms.move("govee-right", room)
        self.refused("has_lights", self.rooms.delete, room, ["govee-left", "govee-right"])
        self.refused("has_sync_box", self.rooms.delete, FIRST_ROOM, [])
        # A disconnected light's row goes with the room.
        self.rooms.delete(room, ["govee-left"])
        self.assertEqual(self.rooms.index(), {"version": 1, "rooms": [FIRST_ROOM],
                                              "lights": {}, "sync": FIRST_ROOM,
                                              "default": FIRST_ROOM})
        self.assertFalse((self.state / "rooms" / (room + ".json")).exists())
        self.refused("last_room", self.rooms.delete, FIRST_ROOM, [])

    def test_the_sync_box_moves_between_rooms(self):
        room = self.rooms.create("Bedroom")
        self.rooms.move("govee-right", room)
        self.rooms.place_sync_box(room)
        self.assertTrue(self.rooms.enabled("govee-right"))
        self.assertFalse(self.rooms.enabled("govee-left"))
        self.rooms.place_sync_box(None)
        self.assertIsNone(self.rooms.sync_profile())
        self.refused("unknown_room", self.rooms.place_sync_box, "room-ffffffff")

    def test_restore_puts_back_the_rooms_and_their_layouts(self):
        room = self.rooms.create("Bedroom")
        state = self.rooms.state()
        self.rooms.move("govee-right", room)
        extra = self.rooms.create("Hall")
        self.rooms.restore(state)
        self.assertEqual(self.rooms.index(), state["index"])
        self.assertEqual(self.rooms.store(FIRST_ROOM).snapshot(), state["layouts"][FIRST_ROOM])
        self.assertEqual(self.rooms.store(room).snapshot()["devices"], [])
        self.assertFalse((self.state / "rooms" / (extra + ".json")).exists())

    def test_restore_removes_a_layout_that_the_change_created(self):
        room = self.rooms.create("Bedroom")
        (self.state / "rooms" / (room + ".json")).unlink()
        state = self.rooms.state()
        self.assertIsNone(state["layouts"][room])
        self.rooms.move("govee-right", room)
        self.assertTrue((self.state / "rooms" / (room + ".json")).exists())
        self.rooms.restore(state)
        self.assertFalse((self.state / "rooms" / (room + ".json")).exists())
        self.assertIsNone(self.rooms.store(room).snapshot())
        self.assertEqual(self.rooms.room_of("govee-right"), FIRST_ROOM)

    def test_restore_returns_to_the_single_layout(self):
        before = (self.state / "desk-layout.json").read_text()
        state = self.rooms.state()
        self.rooms.place_sync_box(None)
        self.rooms.create("Bedroom")
        self.rooms.restore(state)
        self.assertFalse((self.state / "rooms.json").exists())
        self.assertEqual(list((self.state / "rooms").glob("*.json")), [])
        self.assertEqual(json.loads((self.state / "desk-layout.json").read_text()),
                         json.loads(before))
        self.assertEqual(self.rooms.index()["sync"], FIRST_ROOM)
        self.assertEqual(len(self.rooms.store(FIRST_ROOM).snapshot()["devices"]), 6)


if __name__ == "__main__":
    unittest.main()
