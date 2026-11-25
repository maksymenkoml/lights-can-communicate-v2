"""Edited Hue app scenes keep their light actions (see bridge/scene_edits.py)."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge"))
import scene_edits  # noqa: E402


class Light:
    def __init__(self, id_v1):
        self.id_v1, self.id_v2 = id_v1, "light-" + id_v1


class Scene:
    def __init__(self, states):
        self.lightstates = dict(states)


def action(rid, **fields):
    return {"target": {"rid": rid, "rtype": "light"}, "action": fields}


class SceneEditTests(unittest.TestCase):
    def setUp(self):
        self.lights = {slot: Light(slot) for slot in ("1", "2")}

    def test_an_edit_replaces_the_stored_state_so_an_old_white_goes(self):
        scene = Scene({self.lights["2"]: {"on": True, "bri": 198, "ct": 346},
                       self.lights["1"]: {"on": True, "bri": 100}})
        gradient = {"points": [{"color": {"xy": {"x": 0.2, "y": 0.3}}}] * 2}
        changed = scene_edits.apply_actions(scene, [
            action("light-2", on={"on": True}, dimming={"brightness": 50.0},
                   color={"xy": {"x": 0.6, "y": 0.3}}),
            action("light-1", on={"on": False}, gradient=gradient),
        ], self.lights)
        self.assertEqual(changed, 2)
        self.assertEqual(scene.lightstates[self.lights["2"]],
                         {"on": True, "bri": 127, "xy": [0.6, 0.3]})
        self.assertEqual(scene.lightstates[self.lights["1"]], {"on": False, "gradient": gradient})

    def test_a_white_action_stores_its_temperature(self):
        scene = Scene({})
        scene_edits.apply_actions(scene, [action("light-1", color_temperature={"mirek": 250})],
                                  self.lights)
        self.assertEqual(scene.lightstates[self.lights["1"]], {"ct": 250})

    def test_unknown_lights_other_targets_and_bad_items_change_nothing(self):
        before = {self.lights["1"]: {"on": True}}
        scene = Scene(before)
        changed = scene_edits.apply_actions(scene, [
            action("light-9", on={"on": False}),
            {"target": {"rid": "light-1", "rtype": "grouped_light"},
             "action": {"on": {"on": False}}},
            {"target": "light-1"}, "x", action("light-1"),
            action("light-1", on={"on": True}),
        ], self.lights)
        self.assertEqual((changed, scene.lightstates), (0, before))
        self.assertEqual(scene_edits.apply_actions(scene, None, self.lights), 0)


if __name__ == "__main__":
    unittest.main()
