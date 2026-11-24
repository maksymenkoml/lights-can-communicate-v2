"""One Room group of the virtual bridge per room (#78 stage 3)."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
import weakref
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge"))
import room_groups  # noqa: E402
from desk_layout import validate_layout  # noqa: E402
from rooms import FIRST_ROOM, Rooms  # noqa: E402
from test_desk_layout import layout  # noqa: E402
from test_restart_recovery_bootstrap import DRIVER  # noqa: E402
from test_virtual_lights import HUE, THIRD, registry  # noqa: E402

SECOND = "room-0a1b2c3d"
THIRD_ROOM = "room-0b1c2d3e"
NAMESPACE = "smart-lighting-probe"


class Light:
    def __init__(self, id_v1):
        self.id_v1 = id_v1


class Group:
    def __init__(self, data):
        self.id_v1, self.id_v2 = data["id_v1"], data["id_v2"]
        self.name, self.type, self.icon_class = data["name"], data["type"], data["class"]
        self.lights = []

    def add_light(self, light):
        self.lights.append(weakref.ref(light))


class Scene:
    def __init__(self, group, states):
        self.group = weakref.ref(group)
        # As in diyHue, a group scene shares its group's light list.
        self.lights = group.lights
        self.lightstates = weakref.WeakKeyDictionary(states)


def bridge(slots="1234567"):
    lights = {slot: Light(slot) for slot in slots}
    office = Group({"id_v1": "1", "id_v2": "office", "name": "Office", "type": "Room",
                    "class": "Office"})
    for light in lights.values():
        office.add_light(light)
    area = Group({"id_v1": "90", "id_v2": "area", "name": "Area", "type": "Entertainment",
                  "class": "Other"})
    scene = Scene(office, {light: {"on": True} for light in lights.values()})
    return {"lights": lights, "groups": {"1": office, "90": area}, "scenes": {"1": scene}}


def slots_of(group):
    return sorted(reference().id_v1 for reference in group.lights)


class PlanTests(unittest.TestCase):
    def test_the_first_room_keeps_group_one_and_a_new_room_takes_the_lowest_free_id(self):
        groups, deleted = room_groups.plan([FIRST_ROOM, SECOND], {FIRST_ROOM: "1"},
                                           {"0", "1", "2", "90"})
        self.assertEqual((groups, deleted), ({FIRST_ROOM: "1", SECOND: "3"}, []))
        # A recorded group stays; a gone room's group is deleted.
        groups, deleted = room_groups.plan([SECOND], {FIRST_ROOM: "1", SECOND: "3"},
                                           {"0", "1", "3", "90"})
        self.assertEqual((groups, deleted), ({SECOND: "3"}, ["1"]))
        # Group 1 is never given to another room.
        groups, _ = room_groups.plan([THIRD_ROOM], {FIRST_ROOM: "1"}, {"0", "90"})
        self.assertEqual(groups, {THIRD_ROOM: "2"})

    def test_lights_are_members_of_their_rooms(self):
        rooms = {"7": THIRD, "9": HUE, "1": "govee-left"}
        result = room_groups.members(
            [FIRST_ROOM, SECOND], lambda light: SECOND if light in (THIRD, HUE) else FIRST_ROOM,
            rooms)
        self.assertEqual(result, {FIRST_ROOM: ["1"], SECOND: ["7", "9"]})

    def test_the_recorded_groups_survive_and_a_bad_record_is_ignored(self):
        with tempfile.TemporaryDirectory(prefix="tmp_room_groups_") as folder:
            self.assertEqual(room_groups.load_owned(folder, FIRST_ROOM), {FIRST_ROOM: "1"})
            room_groups.save_owned(folder, {FIRST_ROOM: "1", SECOND: "2"})
            self.assertEqual(room_groups.load_owned(folder, FIRST_ROOM),
                             {FIRST_ROOM: "1", SECOND: "2"})
            Path(folder, room_groups.GROUPS_FILE).write_text(json.dumps(
                {SECOND: "1", THIRD_ROOM: "x", "office": "4", "room-0c0c0c0c": "5"}))
            self.assertEqual(room_groups.load_owned(folder, FIRST_ROOM),
                             {FIRST_ROOM: "1", "room-0c0c0c0c": "5"})
            Path(folder, room_groups.GROUPS_FILE).write_text("{not json")
            self.assertEqual(room_groups.load_owned(folder, FIRST_ROOM), {FIRST_ROOM: "1"})


class ApplyTests(unittest.TestCase):
    def test_a_light_moves_to_its_rooms_group_and_leaves_the_other_rooms_scenes(self):
        config = bridge()
        room_groups.apply(
            config, Group, NAMESPACE, {FIRST_ROOM: "1", SECOND: "2"}, [],
            {FIRST_ROOM: ["1", "2", "3", "4", "5"], SECOND: ["6"]},
            {FIRST_ROOM: "Office", SECOND: "Bedroom"})
        bedroom = config["groups"]["2"]
        self.assertEqual((bedroom.name, bedroom.type, bedroom.icon_class),
                         ("Bedroom", "Room", "Other"))
        self.assertEqual(bedroom.id_v2, str(uuid.uuid5(uuid.NAMESPACE_URL,
                                                       f"{NAMESPACE}/{SECOND}")))
        self.assertEqual(slots_of(bedroom), ["6"])
        # Slot 7 is a removed light's virtual light: it stays where it was.
        self.assertEqual(slots_of(config["groups"]["1"]), ["1", "2", "3", "4", "5", "7"])
        scene = config["scenes"]["1"]
        self.assertEqual(sorted(light.id_v1 for light in scene.lightstates),
                         ["1", "2", "3", "4", "5", "7"])
        self.assertIs(scene.lights, config["groups"]["1"].lights)
        # The Entertainment area does not change.
        self.assertEqual(config["groups"]["90"].lights, [])
        # A second start changes nothing.
        room_groups.apply(
            config, Group, NAMESPACE, {FIRST_ROOM: "1", SECOND: "2"}, [],
            {FIRST_ROOM: ["1", "2", "3", "4", "5"], SECOND: ["6"]},
            {FIRST_ROOM: "Office", SECOND: "Bedroom"})
        self.assertIs(config["groups"]["2"], bedroom)
        self.assertEqual(slots_of(bedroom), ["6"])
        self.assertEqual(slots_of(config["groups"]["1"]), ["1", "2", "3", "4", "5", "7"])

    def test_a_deleted_room_takes_its_group_and_its_scenes(self):
        config = bridge()
        room_groups.apply(config, Group, NAMESPACE, {FIRST_ROOM: "1", SECOND: "2"}, [],
                          {FIRST_ROOM: ["1"], SECOND: ["6"]}, {SECOND: "Bedroom"})
        config["scenes"]["2"] = Scene(config["groups"]["2"], {config["lights"]["6"]: {}})
        room_groups.apply(config, Group, NAMESPACE, {FIRST_ROOM: "1"}, ["2"],
                          {FIRST_ROOM: ["1", "6"]}, {})
        self.assertNotIn("2", config["groups"])
        self.assertEqual(list(config["scenes"]), ["1"])
        self.assertIn("6", slots_of(config["groups"]["1"]))
        # A room without a layout name keeps the group's name.
        self.assertEqual(config["groups"]["1"].name, "Office")

    def test_a_renamed_room_renames_its_group(self):
        config = bridge()
        self.assertTrue(room_groups.rename(config, {FIRST_ROOM: "1"}, {FIRST_ROOM: "Study"}))
        self.assertEqual(config["groups"]["1"].name, "Study")
        self.assertFalse(room_groups.rename(config, {FIRST_ROOM: "1"}, {FIRST_ROOM: "Study"}))
        self.assertFalse(room_groups.rename(config, {FIRST_ROOM: "1"}, {}))


class RoomStateTests(unittest.TestCase):
    def test_names_and_signature_come_from_the_rooms(self):
        with tempfile.TemporaryDirectory(prefix="tmp_room_groups_") as folder:
            state = Path(folder)
            rooms = Rooms(state)
            before = room_groups.signature(rooms)
            self.assertEqual(room_groups.room_names(rooms), {})
            write_rooms(state, {THIRD: SECOND})
            self.assertEqual(room_groups.room_names(rooms),
                             {FIRST_ROOM: "Office", SECOND: "Bedroom"})
            self.assertNotEqual(room_groups.signature(rooms), before)
            # The order of the rooms does not restart the bridge.
            moved = room_groups.signature(rooms)
            rooms.reorder([SECOND, FIRST_ROOM])
            self.assertEqual(room_groups.signature(rooms), moved)


def write_rooms(state, lights):
    """Two rooms: the first with every other light, the bedroom with ``lights``."""
    (state / "rooms").mkdir(exist_ok=True)
    for room, name in ((FIRST_ROOM, "Office"), (SECOND, "Bedroom")):
        data = validate_layout(layout())
        data["room"] = name
        data["devices"] = [row for row in data["devices"] if room == FIRST_ROOM]
        (state / "rooms" / (room + ".json")).write_text(json.dumps(data))
    (state / "rooms.json").write_text(json.dumps({
        "version": 1, "rooms": [FIRST_ROOM, SECOND], "lights": lights, "sync": FIRST_ROOM}))


ROOMS_FIXTURE = r'''
def fixture_request(g):
    import uuid
    lights = g["config"]["lights"]
    groups = g["config"]["groups"]
    scenes = g["config"]["scenes"]
    client = g["app"].test_client()
    headers = {"hue-application-key": g["api_key"]}
    def members(group_id):
        return sorted((reference().id_v1 for reference in groups[group_id].lights
                       if reference()), key=int)
    assert members("90") == ["1", "2", "3", "4", "5", "6"], members("90")
    if phase == "one":
        assert members("1") == ["1", "2", "3", "4", "5", "6", "7", "8"], members("1")
        room = client.get("/clip/v2/resource/room", headers=headers).get_json()["data"][0]
        body = {"metadata": {"name": "Evening"},
                "group": {"rid": room["id"], "rtype": "room"},
                "actions": [{"target": {"rid": lights[slot].id_v2, "rtype": "light"},
                             "action": {"on": {"on": True}}}
                            for slot in ("1", "7", "8")]}
        response = client.post("/clip/v2/resource/scene", json=body, headers=headers)
        assert response.status_code == 200, response.get_json()
        g["configManager"].bridgeConfig.save_config(backup=False)
    elif phase in ("two", "again"):
        second = g["room_group_ids"]["room-0a1b2c3d"]
        assert second == "2", g["room_group_ids"]
        group = groups["2"]
        assert group.type == "Room" and group.name == "Bedroom", (group.type, group.name)
        assert group.id_v2 == str(uuid.uuid5(uuid.NAMESPACE_URL,
                                             "smart-lighting-probe/room-0a1b2c3d"))
        assert members("2") == ["7", "8"], members("2")
        assert members("1") == ["1", "2", "3", "4", "5", "6"], members("1")
        assert groups["1"].name == "Office", groups["1"].name
        evening = [scene for scene in scenes.values() if scene.name == "Evening"]
        assert len(evening) == 1
        # diyHue adds the current state of its group's lights when it loads a group
        # scene; a light of another room is never among them.
        stored = sorted(light.id_v1 for light in evening[0].lightstates)
        assert "1" in stored and "7" not in stored and "8" not in stored, stored
        rooms = client.get("/clip/v2/resource/room", headers=headers).get_json()["data"]
        assert sorted(room["metadata"]["name"] for room in rooms) == ["Bedroom", "Office"]
        bedroom = [room for room in rooms if room["metadata"]["name"] == "Bedroom"][0]
        assert len(bedroom["children"]) == 2, bedroom["children"]
        saved = json.loads((state / "room-groups.json").read_text())
        assert saved == {"room-00000001": "1", "room-0a1b2c3d": "2"}, saved
        if phase == "two":
            body = {"metadata": {"name": "Night"},
                    "group": {"rid": bedroom["id"], "rtype": "room"},
                    "actions": [{"target": {"rid": lights["7"].id_v2, "rtype": "light"},
                                 "action": {"on": {"on": False}}}]}
            response = client.post("/clip/v2/resource/scene", json=body, headers=headers)
            assert response.status_code == 200, response.get_json()
            g["configManager"].bridgeConfig.save_config(backup=False)
        else:
            assert [scene.name for scene in scenes.values()].count("Night") == 1
    else:
        assert "2" not in groups, sorted(groups)
        assert members("1") == ["1", "2", "3", "4", "5", "6", "7", "8"], members("1")
        assert not [scene for scene in scenes.values() if scene.name == "Night"]
        assert [scene.name for scene in scenes.values()].count("Evening") == 1
        saved = json.loads((state / "room-groups.json").read_text())
        assert saved == {"room-00000001": "1"}, saved
    print("Room groups PASS " + phase, flush=True)
'''


@unittest.skipUnless(Path("/opt/hue-emulator/HueEmulator3.py").exists(),
                     "Requires the pinned public diyHue image")
class RoomGroupsBootstrapTests(unittest.TestCase):
    def test_rooms_become_room_groups_that_survive_restarts_and_deletion(self):
        start = DRIVER.index("def fixture_request(g):")
        end = DRIVER.index("def signal_main(g):")
        driver = DRIVER[:start] + ROOMS_FIXTURE + "\n\n" + DRIVER[end:]
        source = Path(__file__).resolve().parents[1] / "bridge/bootstrap.py"
        with tempfile.TemporaryDirectory(prefix="tmp_room_groups_bootstrap_") as folder:
            state = Path(folder)
            path = state / "tmp_room_driver.py"
            path.write_text(driver)
            registry(hue=True).save(state / "lights.json")
            for phase in ("one", "two", "again", "gone"):
                with self.subTest(phase=phase):
                    if phase == "two":
                        write_rooms(state, {THIRD: SECOND, HUE: SECOND})
                    elif phase == "gone":
                        (state / "rooms.json").write_text(json.dumps({
                            "version": 1, "rooms": [FIRST_ROOM], "lights": {},
                            "sync": FIRST_ROOM}))
                    result = subprocess.run(
                        [sys.executable, "-B", str(path), phase, folder, str(source)],
                        cwd=folder, capture_output=True, text=True, timeout=30,
                        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
                    self.assertEqual(result.returncode, 0,
                                     result.stderr[-4000:] + result.stdout[-1000:])
                    self.assertIn("Room groups PASS " + phase, result.stdout)


if __name__ == "__main__":
    unittest.main()
