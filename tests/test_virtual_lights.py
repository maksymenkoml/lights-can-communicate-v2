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
HUE = "device-" + "e" * 32
STRIP = "device-" + "f" * 32
STRIP_LIGHT = "a0000000-0000-4000-8000-00000000000f"
HUE_LIGHT = "a0000000-0000-4000-8000-00000000000a"
HUE_SERVICE = "b0000000-0000-4000-8000-00000000000b"


def registry(*, third_model="H6008", fourth=False, remove_third=False, hue=False, strip=False):
    result = LightRegistry.from_dict(sample()).with_govee_light(
        THIRD, "CC:CC:CC:CC:CC:CC:CC:CC", "192.168.10.52", third_model)
    if fourth:
        result = result.with_govee_light(FOURTH, "DD:DD:DD:DD:DD:DD:DD:DD", "192.168.10.53",
                                         "H6076")
    if hue:
        result = result.with_hue_light(HUE, sample()["hue"]["area_id"], HUE_LIGHT, HUE_SERVICE,
                                       3, "LCX005")
    if strip:
        # A gradient strip in a room without the Sync Box: for scenes only.
        result = result.with_scene_hue_light(STRIP, STRIP_LIGHT, "LCX005", "room")
    if remove_third:
        result = result.without_light(THIRD)
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


class HuePlanTests(unittest.TestCase):
    def test_a_hue_light_enrolled_later_gets_its_slot_and_channel_count(self):
        self.assertEqual(virtual_lights.hue_plan(None), [])
        self.assertEqual(virtual_lights.hue_plan(registry().to_dict()), [])
        lights = virtual_lights.hue_plan(registry(fourth=True, hue=True).to_dict())
        self.assertEqual(lights, [{"slot": "9", "light": HUE, "seed": HUE, "model": "LCX005",
                                   "channels": 3, "gradient": True}])
        self.assertEqual(virtual_lights.default_name(lights[0]), "Hue LCX005 eeee")
        # Govee planning is unchanged by the Hue light.
        self.assertEqual(virtual_lights.plan(registry(fourth=True, hue=True).to_dict()),
                         virtual_lights.plan(registry(fourth=True).to_dict()))

    def test_a_gradient_model_is_a_gradient_light_also_for_scenes_only(self):
        # A Hue gradient strip in a room without the Sync Box has no channels; its
        # model still makes it a gradient light, as the Hue app should show it.
        base = registry().to_dict()
        cases = (("LCX005", True), ("LCX006", True), ("440400982841", False),
                 ("unknown", False))
        for model, gradient in cases:
            with self.subTest(model=model):
                data = LightRegistry.from_dict(base).with_scene_hue_light(
                    HUE, HUE_LIGHT, model, "room").to_dict()
                (light,) = virtual_lights.hue_plan(data)
                self.assertEqual((light["channels"], light["gradient"]), (0, gradient))
        # Several streamed channels make a gradient light whatever the model says.
        data = LightRegistry.from_dict(base).with_hue_light(
            HUE, sample()["hue"]["area_id"], HUE_LIGHT, HUE_SERVICE, 3, "unknown").to_dict()
        self.assertTrue(virtual_lights.hue_plan(data)[0]["gradient"])

    def test_the_signature_follows_an_enrolled_hue_light(self):
        with tempfile.TemporaryDirectory(prefix="tmp_virtual_signature_") as folder:
            state = Path(folder)
            registry(fourth=True).save(state / "lights.json")
            before = virtual_lights.signature(state)
            registry(fourth=True, hue=True).save(state / "lights.json")
            self.assertNotEqual(virtual_lights.signature(state), before)


class MigratedHueTests(unittest.TestCase):
    def test_a_removed_migrated_hue_light_is_no_longer_driven(self):
        self.assertEqual(virtual_lights.migrated_hue_slots(None),
                         virtual_lights.MIGRATED_HUE_SLOTS)
        removed = LightRegistry.from_dict(sample()).without_light("play-center")
        slots = virtual_lights.migrated_hue_slots(removed.to_dict())
        self.assertEqual(slots, {"3": "hue-monitor", "4": "play-left", "6": "play-right"})
        output = BridgeOutput("http://adapter", "t", sender=lambda value: None,
                              include_strip=True, include_hue_play=True, migrated_hue=slots)
        self.assertFalse(output.submit("5", {"on": True}, 0))
        self.assertTrue(output.submit("4", {"on": True}, 0))
        self.assertEqual(set(output.slots.values()) - {"govee-left", "govee-right"},
                         {"hue-monitor", "play-left", "play-right"})
        without_strip = virtual_lights.migrated_hue_slots(
            LightRegistry.from_dict(sample()).without_light("hue-monitor").to_dict())
        output = BridgeOutput("http://adapter", "t", sender=lambda value: None,
                              include_strip=True, include_hue_play=True,
                              migrated_hue=without_strip)
        self.assertFalse(output.submit("3", {"on": True}, 0))

    def test_the_signature_follows_a_removed_migrated_light(self):
        with tempfile.TemporaryDirectory(prefix="tmp_virtual_signature_") as folder:
            state = Path(folder)
            LightRegistry.from_dict(sample()).save(state / "lights.json")
            before = virtual_lights.signature(state)
            LightRegistry.from_dict(sample()).without_light("play-left").save(
                state / "lights.json")
            self.assertNotEqual(virtual_lights.signature(state), before)


class SlotMapTests(unittest.TestCase):
    SLOTS = {"1": "govee-left", "2": "govee-right", "7": THIRD}

    def test_bridge_output_sends_an_enrolled_slot_by_light_id(self):
        sent = []
        output = BridgeOutput("http://adapter", "t", sender=sent.append, include_strip=True,
                              include_hue_play=True, govee_slots=self.SLOTS)
        self.assertTrue(output.submit("7", {"on": True}, 0))
        self.assertEqual(output.pending[THIRD]["light"], THIRD)
        self.assertEqual(output.hue_roles,
                         {"hue-monitor", "play-left", "play-center", "play-right"})
        self.assertFalse(output.submit("8", {"on": True}, 0))
        legacy = BridgeOutput("http://adapter", "t")
        self.assertEqual(legacy.slots, {"1": "govee-left", "2": "govee-right"})

    def test_bridge_output_routes_an_enrolled_hue_slot_as_a_native_light(self):
        output = BridgeOutput("http://adapter", "t", sender=lambda payload: None,
                              include_strip=True, include_hue_play=True,
                              govee_slots=self.SLOTS, hue_slots={"9": HUE})
        self.assertIn(HUE, output.hue_roles)
        self.assertNotIn(THIRD, output.hue_roles)
        self.assertTrue(output.submit("9", {"on": True, "xy": [0.3, 0.3]}, 0))
        self.assertEqual(output.pending[HUE]["state"]["_changed"], ["xy"])

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
    if phase in ("enrolled", "rebuilt"):
        strip = lights["10"]
        assert strip in room and strip not in area
        assert strip.modelid == "LCX006", strip.modelid
        assert strip.protocol_cfg["points_capable"] == 5
        assert len(strip.state["gradient"]["points"]) == 5
    if phase == "enrolled":
        # The color bulb an earlier gateway made for the strip: the next start
        # turns it into a gradient light in place.
        state.joinpath("strip-id.txt").write_text(strip.uniqueid)
        strip.modelid = "LCT015"
        strip.config["archetype"] = "sultanbulb"
        strip.protocol_cfg = {"proxy_color_kind": "xy"}
        strip.state.pop("gradient")
        g["configManager"].bridgeConfig.save_config(backup=False)
    if phase == "rebuilt":
        # The same saved light, not a new one (a new light gets a new unique id).
        assert strip.uniqueid == state.joinpath("strip-id.txt").read_text()
        assert strip.config["archetype"] == "huelightstrip"
        assert set(g["selected_slots"]) == {"1", "2", "3", "4", "5", "6", "7", "8", "9", "10"}
    elif phase == "enrolled":
        assert set(g["selected_slots"]) == {"1", "2", "3", "4", "5", "6", "7", "8", "9", "10"}
        third, fourth, hue = lights["7"], lights["8"], lights["9"]
        assert third in room and fourth in room and hue in room
        assert hue not in area
        assert hue.modelid == "LCX006", hue.modelid
        assert hue.protocol_cfg["proxy_color_kind"] == "gradient"
        assert hue.state["reachable"] is True
        assert g["hue_slots"] == {"9": "device-" + "e" * 32, "10": "device-" + "f" * 32}
        assert third.modelid == "LCT015", third.modelid
        assert fourth.modelid == "915005987201", fourth.modelid
        assert lights["1"].modelid == "915005987201"
        assert third.state["reachable"] is True
        assert g["govee_slots"]["7"] == "device-" + "c" * 32
        state.joinpath("third-id.txt").write_text(third.id_v2)
    elif phase == "missing":
        # Without a registry only the migrated lamps are driven and reachable.
        assert set(g["selected_slots"]) == {"1", "2", "3", "4", "5", "6"}
        assert lights["9"].state["reachable"] is False
        assert lights["7"].state["reachable"] is False
        assert lights["8"].state["reachable"] is False
        assert lights["10"].state["reachable"] is False
        assert lights["1"].state["reachable"] is True
    else:
        assert set(g["selected_slots"]) == {"1", "2", "3", "4", "5", "6", "8", "9", "10"}
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
        # Phases: enrolled, one light removed, then the registry file deleted.
        start = DRIVER.index("def fixture_request(g):")
        end = DRIVER.index("def signal_main(g):")
        driver = DRIVER[:start] + VIRTUAL_FIXTURE + "\n\n" + DRIVER[end:]
        source = Path(__file__).resolve().parents[1] / "bridge/bootstrap.py"
        with tempfile.TemporaryDirectory(prefix="tmp_virtual_lights_bootstrap_") as folder:
            state = Path(folder)
            path = state / "tmp_virtual_driver.py"
            path.write_text(driver)
            for phase, current in (("enrolled", registry(fourth=True, hue=True, strip=True)),
                                   ("rebuilt", registry(fourth=True, hue=True, strip=True)),
                                   ("removed", registry(fourth=True, remove_third=True,
                                                        hue=True, strip=True)),
                                   ("missing", None)):
                with self.subTest(phase=phase):
                    if current is None:
                        (state / "lights.json").unlink()
                    else:
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
