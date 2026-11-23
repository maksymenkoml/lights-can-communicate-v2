"""Virtual bridge lights per enrolled light (#7 stage 4)."""

import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge"))
import virtual_lights  # noqa: E402
from bridge_output import BridgeOutput  # noqa: E402
from hue_power_requests import HuePowerRequests  # noqa: E402
from power_feedback import PowerFeedback  # noqa: E402
from scene_play_intent import ScenePlayIntent  # noqa: E402
from test_light_registry import sample  # noqa: E402
from test_restart_recovery_bootstrap import DRIVER  # noqa: E402

from lights_can_communicate_v2_core.light_registry import LightRegistry  # noqa: E402

THIRD = "device-" + "c" * 32
FOURTH = "device-" + "d" * 32


def registry(*, third_model="H6008", fourth=False, remove_third=False):
    result = LightRegistry.from_dict(sample()).with_govee_light(
        THIRD, "CC:CC:CC:CC:CC:CC:CC:CC", "192.168.10.52", third_model)
    if fourth:
        result = result.with_govee_light(FOURTH, "DD:DD:DD:DD:DD:DD:DD:DD", "192.168.10.53",
                                         "H6076")
    if remove_third:
        result = result.without_govee_light(THIRD)
    return result


class PlanTests(unittest.TestCase):
    def test_without_a_registry_the_migrated_lamps_keep_slots_one_and_two(self):
        lights, retired = virtual_lights.plan(None)
        self.assertEqual([(item["slot"], item["light"], item["seed"]) for item in lights],
                         [("1", "govee-left", "Left"), ("2", "govee-right", "Right")])
        self.assertEqual(retired, frozenset())

    def test_the_migrated_registry_matches_the_legacy_plan(self):
        # Migrated lamps carry no model: they stay segmented, so nothing restarts.
        data = sample()
        for item in data["lights"][:2]:
            item["govee"].pop("sku", None)
            item["govee"].pop("model", None)
        self.assertEqual(virtual_lights.plan(data), virtual_lights.plan(None))

    def test_enrolled_lights_take_their_registry_slot_seed_and_tier(self):
        lights, retired = virtual_lights.plan(registry(fourth=True).to_dict())
        self.assertEqual([(item["slot"], item["light"], item["seed"], item["segmented"])
                          for item in lights],
                         [("1", "govee-left", "Left", True), ("2", "govee-right", "Right", True),
                          ("7", THIRD, THIRD, False), ("8", FOURTH, FOURTH, True)])
        self.assertEqual(virtual_lights.default_name(lights[2]), "Govee H6008 cccc")
        lights, retired = virtual_lights.plan(registry(fourth=True, remove_third=True).to_dict())
        self.assertEqual([item["slot"] for item in lights], ["1", "2", "8"])
        self.assertEqual(retired, frozenset({"7"}))

    def test_the_signature_follows_lights_but_not_addresses(self):
        with tempfile.TemporaryDirectory(prefix="tmp_virtual_lights_") as folder:
            state = Path(folder)
            self.assertEqual(virtual_lights.signature(state), virtual_lights.signature(state))
            legacy = virtual_lights.signature(state)
            current = registry()
            current.save(state / "lights.json")
            enrolled = virtual_lights.signature(state)
            self.assertNotEqual(enrolled, legacy)
            moved = current.govee_targets()
            moved[-1]["ip"] = "192.168.10.99"
            current.with_govee_targets(moved).save(state / "lights.json")
            self.assertEqual(virtual_lights.signature(state), enrolled)
            (state / "lights.json").write_text("{")
            self.assertEqual(virtual_lights.signature(state), legacy)


class SlotMapTests(unittest.TestCase):
    SLOTS = {"1": "govee-left", "2": "govee-right", "7": THIRD}

    def test_bridge_output_sends_an_enrolled_slot_by_light_id(self):
        sent = []
        output = BridgeOutput("http://adapter", "t", sender=sent.append, include_strip=True,
                              include_hue_play=True, govee_slots=self.SLOTS)
        self.assertTrue(output.submit("7", {"on": True}, 0))
        self.assertEqual(output.pending[THIRD]["light"], THIRD)
        self.assertEqual(output.hue_roles, {"strip", "hue_left", "hue_center", "hue_right"})
        self.assertFalse(output.submit("8", {"on": True}, 0))
        legacy = BridgeOutput("http://adapter", "t")
        self.assertEqual(legacy.slots, {"1": "govee-left", "2": "govee-right"})

    def test_power_maps_take_the_registry_slots(self):
        feedback = PowerFeedback("http://adapter", "t", {}, threading.RLock(), lambda lamp: None,
                                 lambda slots: None, fetcher=dict, slots=self.SLOTS)
        requests = HuePowerRequests("http://adapter", "t", {}, {}, feedback, slots=self.SLOTS)
        self.assertEqual(feedback.SLOTS, self.SLOTS)
        self.assertEqual(requests.SLOTS, self.SLOTS)
        self.assertEqual(PowerFeedback.SLOTS, {"1": "govee-left", "2": "govee-right"})

    def test_scene_intent_keeps_enrolled_slots(self):
        with tempfile.TemporaryDirectory(prefix="tmp_virtual_intent_") as folder:
            intent = ScenePlayIntent(Path(folder) / "intent.json", {"1", "2", "7", "x"},
                                     threading.RLock(), {}, {})
            self.assertEqual(intent.slots, frozenset({"1", "2", "7"}))


VIRTUAL_FIXTURE = r'''
def fixture_request(g):
    lights = g["config"]["lights"]
    groups = g["config"]["groups"]
    room = [reference() for reference in groups["1"].lights]
    area = [reference() for reference in groups["90"].lights]
    assert {lamp.id_v1 for lamp in area} == set("123456"), sorted(lamp.id_v1 for lamp in area)
    if phase == "enrolled":
        assert set(g["selected_slots"]) == {"1", "2", "3", "4", "5", "6", "7", "8"}
        third, fourth = lights["7"], lights["8"]
        assert third in room and fourth in room
        assert third.modelid == "LCT015", third.modelid
        assert fourth.modelid == "915005987201", fourth.modelid
        assert lights["1"].modelid == "915005987201"
        assert third.state["reachable"] is True
        assert g["govee_slots"]["7"] == "device-" + "c" * 32
        state.joinpath("third-id.txt").write_text(third.id_v2)
    else:
        assert set(g["selected_slots"]) == {"1", "2", "3", "4", "5", "6", "8"}
        third = lights["7"]
        assert third.id_v2 == state.joinpath("third-id.txt").read_text()
        assert third.state["reachable"] is False
        assert third in room
    print("Virtual lights PASS " + phase, flush=True)
'''


@unittest.skipUnless(Path("/opt/hue-emulator/HueEmulator3.py").exists(),
                     "Requires the pinned public diyHue image")
class VirtualLightsBootstrapTests(unittest.TestCase):
    def test_enrolled_lights_join_the_room_but_not_the_area_and_survive_removal(self):
        start = DRIVER.index("def fixture_request(g):")
        end = DRIVER.index("def signal_main(g):")
        driver = DRIVER[:start] + VIRTUAL_FIXTURE + "\n\n" + DRIVER[end:]
        source = Path(__file__).resolve().parents[1] / "bridge/bootstrap.py"
        with tempfile.TemporaryDirectory(prefix="tmp_virtual_lights_bootstrap_") as folder:
            state = Path(folder)
            path = state / "tmp_virtual_driver.py"
            path.write_text(driver)
            for phase, current in (("enrolled", registry(fourth=True)),
                                   ("removed", registry(fourth=True, remove_third=True))):
                with self.subTest(phase=phase):
                    current.save(state / "lights.json")
                    result = subprocess.run(
                        [sys.executable, "-B", str(path), phase, folder, str(source)],
                        cwd=folder, capture_output=True, text=True, timeout=30,
                        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
                    self.assertEqual(result.returncode, 0,
                                     result.stderr[-4000:] + result.stdout[-1000:])
                    self.assertIn("Virtual lights PASS " + phase, result.stdout)


if __name__ == "__main__":
    unittest.main()
