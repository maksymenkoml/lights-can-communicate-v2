"""Edited Hue app scenes keep their light actions (see bridge/scene_edits.py)."""

import sys
import unittest
import weakref
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge"))
import scene_edits  # noqa: E402
from scene_edits import InvalidEdit  # noqa: E402


class Light:
    def __init__(self, id_v1):
        self.id_v1, self.id_v2 = id_v1, "light-" + id_v1


class Group:
    def __init__(self, lights):
        self.lights = [weakref.ref(light) for light in lights]


class Scene:
    def __init__(self, states, group=None):
        self.lightstates = weakref.WeakKeyDictionary(states)
        self.group = weakref.ref(group) if group is not None else None


def action(rid, **fields):
    return {"target": {"rid": rid, "rtype": "light"}, "action": fields}


class SceneEditTests(unittest.TestCase):
    def setUp(self):
        self.lights = {slot: Light(slot) for slot in ("1", "2", "3")}
        self.room = Group([self.lights["1"], self.lights["2"]])

    def edit(self, scene, actions):
        return scene_edits.replace(
            scene, scene_edits.edited_states(scene, actions, self.lights))

    def test_an_edit_is_the_whole_list_so_an_old_white_and_an_omitted_light_go(self):
        one, two = self.lights["1"], self.lights["2"]
        scene = Scene({two: {"on": True, "bri": 198, "ct": 346}, one: {"on": True, "bri": 100}},
                      self.room)
        self.assertTrue(self.edit(scene, [action("light-2", on={"on": True},
                                                 dimming={"brightness": 50.0},
                                                 color={"xy": {"x": 0.6, "y": 0.3}})]))
        self.assertEqual(dict(scene.lightstates), {two: {"on": True, "bri": 127, "xy": [0.6, 0.3]}})
        # The same edit again changes nothing.
        self.assertFalse(self.edit(scene, [action("light-2", on={"on": True},
                                                  dimming={"brightness": 50.0},
                                                  color={"xy": {"x": 0.6, "y": 0.3}})]))

    def test_white_and_gradient_actions_are_stored_as_diyhue_stores_them(self):
        gradient = {"points": [{"color": {"xy": {"x": 0.2, "y": 0.3}}}] * 2}
        scene = Scene({}, self.room)
        self.edit(scene, [action("light-1", color_temperature={"mirek": 250}),
                          action("light-2", on={"on": False}, gradient=gradient)])
        self.assertEqual(scene.lightstates[self.lights["1"]], {"ct": 250})
        self.assertEqual(scene.lightstates[self.lights["2"]], {"on": False, "gradient": gradient})

    def test_a_stored_gradient_keeps_only_checked_points_and_a_known_mode(self):
        point = {"color": {"xy": {"x": 0.2, "y": 0.3}, "extra": "x" * 1000}, "junk": [1] * 100}
        scene = Scene({}, self.room)
        self.edit(scene, [action("light-1", gradient={"points": [point, point], "mode":
                                                      "random_pixelated", "junk": "x" * 1000}),
                          action("light-2", gradient={"points": [point, point], "mode": "x" * 50})])
        clean = {"color": {"xy": {"x": 0.2, "y": 0.3}}}
        self.assertEqual(scene.lightstates[self.lights["1"]],
                         {"gradient": {"points": [clean, clean], "mode": "random_pixelated"}})
        self.assertEqual(scene.lightstates[self.lights["2"]],
                         {"gradient": {"points": [clean, clean]}})

    def test_a_replacement_is_published_whole(self):
        scene = Scene({self.lights["1"]: {"on": True}}, self.room)
        old = scene.lightstates
        self.edit(scene, [action("light-2", on={"on": True})])
        self.assertEqual(dict(old), {self.lights["1"]: {"on": True}}, "a reader keeps the old map")
        self.assertEqual(dict(scene.lightstates), {self.lights["2"]: {"on": True}})

    def test_a_room_scene_refuses_a_light_of_another_room_and_keeps_its_states(self):
        before = {self.lights["1"]: {"on": True}}
        scene = Scene(before, self.room)
        with self.assertRaises(InvalidEdit):
            self.edit(scene, [action("light-1", on={"on": False}),
                              action("light-3", on={"on": False})])
        self.assertEqual(dict(scene.lightstates), before)
        # A light scene without a room may hold any light.
        free = Scene({})
        self.assertTrue(self.edit(free, [action("light-3", on={"on": True})]))

    def test_a_bad_action_anywhere_changes_nothing(self):
        before = {self.lights["1"]: {"on": True}}
        bad = [
            [action("light-9", on={"on": False})],
            [{"target": {"rid": "light-1", "rtype": "grouped_light"}, "action": {}}],
            [{"target": "light-1"}], ["x"], None,
            [action("light-1", color={"xy": {"x": 0.2}})],
            [action("light-1", dimming={"brightness": float("nan")})],
            [action("light-1", dimming={"brightness": 101})],
            [action("light-1", color_temperature={"mirek": 900})],
            [action("light-1", on={"on": "yes"})],
            [action("light-1", gradient={"points": [{"color": {"xy": {"x": 0.2, "y": 0.3}}}]})],
            [{"target": {"rid": "light-1", "rtype": "light"}, "action": "x"}],
            [{"target": {"rid": ["light-1"], "rtype": "light"}, "action": {}}],
            [{"target": {"rid": {"x": 1}, "rtype": "light"}, "action": {}}],
            [action("light-1", gradient={"points": [{"color": "red"}, {"color": 1}]})],
            [action("light-1", gradient={"points": ["x", "y"]})],
            [action("light-1", dimming={"brightness": 10 ** 400})],
            [action("light-1", dimming={"brightness": True})],
            [action("light-1", dimming=None)],
        ]
        for actions in bad:
            with self.subTest(actions=actions):
                scene = Scene(before, self.room)
                with self.assertRaises(InvalidEdit):
                    self.edit(scene, [action("light-2", on={"on": False})]
                              + (actions if isinstance(actions, list) else [actions]))
                self.assertEqual(dict(scene.lightstates), before)


if __name__ == "__main__":
    unittest.main()
