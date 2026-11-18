"""Pilot test names are replaced on start; owner-chosen names are kept."""

import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "experiments/hue-bridge"))
from hue_names import AREA_NAME, BRIDGE_NAME, ROOM_NAME, rename_legacy  # noqa: E402


def resource(name):
    return types.SimpleNamespace(name=name, id_v2="unchanged")


class HueNameTests(unittest.TestCase):
    def test_exact_pilot_names_are_replaced_and_identities_kept(self):
        config = {
            "config": {"name": "Lights Can Communicate v2 Probe"},
            "lights": {"1": resource("Govee Left TEST"), "4": resource("Hue Play Center PROXY"),
                       "3": resource("Hue Monitor PROXY")},
            "groups": {"1": resource("Probe Office TEST"), "90": resource("Desk Sync PILOT")},
            "scenes": {"a": resource("Probe Violet Amber TEST"), "b": resource("Vapor wave")},
        }
        rename_legacy(config)
        self.assertEqual(config["config"]["name"], BRIDGE_NAME)
        self.assertEqual([r.name for r in config["lights"].values()],
                         ["Govee Left", "Hue Play Center", "Hue Monitor"])
        self.assertEqual(config["groups"]["1"].name, ROOM_NAME)
        self.assertEqual(config["groups"]["90"].name, AREA_NAME)
        self.assertEqual(config["scenes"]["a"].name, "Violet Amber")
        self.assertEqual(config["scenes"]["b"].name, "Vapor wave")
        resources = [r for key, section in config.items() if key != "config"
                     for r in section.values()]
        self.assertTrue(all(r.id_v2 == "unchanged" for r in resources))
        names = [r.name for r in resources]
        rename_legacy(config)
        self.assertEqual([r.name for r in resources], names)

    def test_names_chosen_in_the_hue_app_are_never_overwritten(self):
        config = {"config": {"name": "Mykola desk bridge"},
                  "lights": {"1": resource("Desk left lamp")},
                  "groups": {"1": resource("Govee Left TEST room")}}
        rename_legacy(config)
        self.assertEqual(config["config"]["name"], "Mykola desk bridge")
        self.assertEqual(config["lights"]["1"].name, "Desk left lamp")
        self.assertEqual(config["groups"]["1"].name, "Govee Left TEST room")

    def test_a_new_bridge_gets_the_production_name(self):
        config = {"config": {"name": "DiyHue Bridge"}}
        rename_legacy(config)
        self.assertEqual(config["config"]["name"], BRIDGE_NAME)

    def test_no_production_name_carries_a_test_marker(self):
        for name in (BRIDGE_NAME, ROOM_NAME, AREA_NAME):
            self.assertNotRegex(name, r"TEST|PILOT|PROXY|Probe")


if __name__ == "__main__":
    unittest.main()
