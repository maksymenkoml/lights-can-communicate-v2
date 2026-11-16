"""Play intent tests; the pinned-image run also exercises public Hue classes."""

import copy
import gc
import json
import sys
import tempfile
import unittest
import uuid
import weakref
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "experiments/hue-bridge"))
from bridge_output import PlayGuard, desk_palette_index  # noqa: E402
from scene_play_intent import MAX_STATE_BYTES, ScenePlayIntent, install_scene_play_intent  # noqa: E402

SCENE_ID = str(uuid.UUID(int=1))
OTHER_ID = str(uuid.UUID(int=2))


class QueuedThread:
    queued = []

    def __init__(self, target, args=()):
        self.target, self.args = target, args

    def start(self):
        self.queued.append(self)

    def run(self):
        self.target(*self.args)


class ScenePlayIntentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="tmp_play-intent-")
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "private" / "scene-play-intent.json"
        self.guard = PlayGuard()
        self.calls = []
        calls = self.calls

        class Lamp:
            def __init__(self, slot):
                self.id_v1, self.modelid = slot, "LCT015"
                self.dynamics = {"status": "none", "status_values": ["dynamic_palette"]}
                self.state = {"on": False}

            def _setV1State(self, state, advertise=True):
                self.state.update(state)
                calls.append((self.id_v1, copy.deepcopy(state)))

            def setV2State(self, state):
                self._setV1State(state)

            def dynamicScenePlay(self, palette, index):
                self.dynamics["status"] = "dynamic_palette"
                self.setV2State(palette["color"][index])

        class Scene:
            def activate(self, values):
                action = values.get("recall", {}).get("action")
                if action == "dynamic_palette":
                    for index, reference in enumerate(self.lights):
                        module.Thread(target=reference().dynamicScenePlay,
                                      args=(self.palette, index)).start()
                elif action != "deactivate":
                    for lamp, state in self.lightstates.items():
                        lamp._setV1State(state)

        self.Lamp = Lamp
        self.lights = {slot: Lamp(slot) for slot in ("1", "2", "3", "7")}
        self.scene = Scene()
        self.scene.id_v2 = SCENE_ID
        self.scene.speed = 0.5
        self.scene.palette = {"color": [{"color": {"xy": {"x": x, "y": 0.3}}}
                                        for x in (0.2, 0.3, 0.4, 0.5, 0.6)]}
        self.scene.lights = [weakref.ref(self.lights[slot]) for slot in ("1", "2", "3")]
        self.scene.lightstates = {self.lights[slot]: {"xy": [0.2, 0.3]}
                                  for slot in ("1", "2", "3")}
        self.scenes = {"legacy-scene-id": self.scene}
        module = SimpleNamespace(Scene=Scene, Thread=self.guard.thread_factory(
            QueuedThread, Lamp.dynamicScenePlay, index_selector=desk_palette_index))
        self.module = module
        QueuedThread.queued = []
        self.install()

    def install(self):
        self.controller = install_scene_play_intent(
            self.module, self.Lamp, self.guard, self.lights, self.scenes,
            {"1", "2", "3"}, self.path)

    def play(self):
        self.scene.activate({"recall": {"action": "dynamic_palette"}})

    def read(self):
        return json.loads(self.path.read_text())["lights"]

    def test_absent_state_is_backward_compatible_and_replay_sends_no_power(self):
        self.assertEqual(self.controller.replay(), [])
        self.assertFalse(self.path.exists())
        self.assertEqual(self.calls, [])

    def test_play_records_only_actual_selected_members_as_atomic_private_state(self):
        self.play()
        self.assertEqual(self.read(), {"1": SCENE_ID, "2": SCENE_ID, "3": SCENE_ID})
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(self.path.parent.glob("tmp_*")), [])
        self.assertNotIn("palette", self.path.read_text())

    def test_cold_reload_replays_partial_members_without_mutating_scene_or_power(self):
        self.play()
        self.lights["2"]._setV1State({"bri": 50})
        before = copy.deepcopy(self.scene.palette)
        members = list(self.scene.lights)
        cold = ScenePlayIntent(self.path, {"1", "2", "3"}, self.guard, self.lights, self.scenes)
        cold.scene_module = self.module
        QueuedThread.queued = []
        self.assertEqual(cold.replay(), ["1", "3"])
        for thread in QueuedThread.queued:
            thread.run()
        self.assertEqual(self.scene.palette, before)
        self.assertEqual(self.scene.lights, members)
        self.assertTrue(all(lamp.state["on"] is False for lamp in self.lights.values()))
        self.assertEqual(self.read(), {"1": SCENE_ID, "3": SCENE_ID})
        self.assertEqual(self.calls[-2][1]["color"]["xy"]["x"], 0.2)
        self.assertEqual(self.calls[-1][1]["color"]["xy"]["x"], 0.2)

    def test_manual_cancellation_does_not_cancel_other_members_and_workers_do_not_cancel(self):
        self.play()
        QueuedThread.queued[0].run()
        self.assertEqual(len(self.read()), 3)
        self.lights["2"]._setV1State({"on": False})
        self.assertEqual(self.read(), {"1": SCENE_ID, "3": SCENE_ID})

    def test_stop_cancels_scene_members_but_static_recall_only_affected_lightstates(self):
        self.play()
        self.scene.lightstates = {self.lights["2"]: {"xy": [0.2, 0.3]}}
        self.scene.activate({"recall": {"action": "active"}})
        self.assertEqual(self.read(), {"1": SCENE_ID, "3": SCENE_ID})
        self.scene.activate({"recall": {"action": "deactivate"}})
        self.assertEqual(self.read(), {})

    def test_power_feedback_and_sync_pause_cache_changes_preserve_intent(self):
        self.play()
        for lamp in self.lights.values():
            lamp.state["on"] = False  # PowerFeedback uses direct state/event projection.
            lamp.streaming = True  # Sync operates on a separate data plane.
            lamp.state["xy"] = [0.6, 0.2]
            lamp.streaming = False
            lamp.state["on"] = True
        self.assertEqual(len(self.read()), 3)

    def test_unknown_slots_scenes_and_removed_members_are_skipped_and_pruned(self):
        self.path.parent.mkdir()
        self.path.write_text(json.dumps({"version": 1, "lights": {
            "1": SCENE_ID, "2": OTHER_ID, "3": SCENE_ID, "7": SCENE_ID, "url": "https://x"}}))
        self.scene.lights = [weakref.ref(self.lights["1"])]
        cold = ScenePlayIntent(self.path, {"1", "2", "3", "7"}, self.guard,
                               self.lights, self.scenes)
        cold.scene_module = self.module
        self.assertEqual(cold.replay(), ["1"])
        self.assertEqual(self.read(), {"1": SCENE_ID})

    def test_invalid_oversize_or_malformed_state_starts_nothing(self):
        self.path.parent.mkdir()
        for raw in (b"{", b"x" * (MAX_STATE_BYTES + 1), b"[]", b'{"version":true,"lights":{}}',
                    b'{"version":1,"lights":{},"command":"on"}',
                    b'{"version":1,"lights":{"1":"not-a-uuid"}}'):
            with self.subTest(raw=raw[:60]):
                self.path.write_bytes(raw)
                cold = ScenePlayIntent(self.path, {"1"}, self.guard, self.lights, self.scenes)
                cold.scene_module = self.module
                self.assertEqual(cold.replay(), [])

    def test_failed_write_drops_stale_disk_intent_without_breaking_manual_change(self):
        self.play()
        with patch("scene_play_intent.os.replace", side_effect=OSError("synthetic")):
            self.lights["2"]._setV1State({"bri": 22})
        self.assertFalse(self.path.exists())
        self.assertEqual(self.controller.last_error, "OSError")
        self.assertEqual(self.lights["2"].state["bri"], 22)
        self.assertEqual(list(self.path.parent.glob("tmp_*")), [])

    def test_replay_keeps_generation_and_layout_guard_and_strips_power_from_palette(self):
        self.play()
        old = QueuedThread.queued[0]
        self.scene.palette["color"][0]["on"] = {"on": True}
        self.assertEqual(self.controller.replay(), ["1", "2", "3"])
        old.run()
        self.assertEqual(self.calls, [])
        for thread in QueuedThread.queued[-3:]:
            thread.run()
        self.assertTrue(all("on" not in state for _, state in self.calls))
        self.assertEqual(self.calls[1][1]["color"]["xy"]["x"], 0.6)
        self.assertEqual(self.scene.palette["color"][0]["on"], {"on": True})

    def test_new_invalid_play_clears_old_intent_and_malformed_palette_never_replays(self):
        self.play()
        self.scene.id_v2 = "not-a-scene-id"
        self.play()
        self.assertEqual(self.read(), {})
        self.scene.id_v2 = SCENE_ID
        self.play()
        for palette in ({"color": []}, {"color": ["invalid"]},
                        {"color": [{"color": {"xy": {"x": "invalid", "y": 0.3}}}]},
                        {"color": [{"on": {"on": True}}]}):
            self.scene.palette = palette
            self.path.write_text(json.dumps({"version": 1, "lights": {"1": SCENE_ID}}))
            cold = ScenePlayIntent(self.path, {"1", "2", "3"}, self.guard,
                                   self.lights, self.scenes)
            cold.scene_module = self.module
            self.assertEqual(cold.replay(), [])
        self.assertEqual(self.read(), {})

    def test_worker_start_failure_never_remembers_new_play(self):
        with patch.object(QueuedThread, "start", side_effect=RuntimeError("synthetic")):
            with self.assertRaises(RuntimeError):
                self.play()
        self.assertEqual(self.controller.intents, {})
        self.assertFalse(self.path.exists())

    def test_write_failure_recovers_on_next_play_and_snapshot_contains_only_counts(self):
        with patch("scene_play_intent.os.replace", side_effect=OSError("synthetic")):
            self.play()
        self.assertFalse(self.path.exists())
        self.play()
        self.assertEqual(self.controller.snapshot(), {
            "remembered_lights": 3, "persisted_lights": 3, "replayed_lights": 0,
            "skipped_lights": 0, "last_error": None})
        self.assertEqual(self.controller.replay(), ["1", "2", "3"])
        self.assertEqual(self.controller.snapshot()["replayed_lights"], 3)

    def test_different_scenes_remain_independent_per_light(self):
        self.play()
        other = copy.copy(self.scene)
        other.id_v2 = OTHER_ID
        other.lights = [weakref.ref(self.lights["2"])]
        other.lightstates = {self.lights["2"]: {"xy": [0.4, 0.3]}}
        self.scenes["other"] = other
        other.activate({"recall": {"action": "dynamic_palette"}})
        self.assertEqual(self.read(), {"1": SCENE_ID, "2": OTHER_ID, "3": SCENE_ID})
        self.assertEqual(self.controller.replay(), ["1", "3", "2"])
        other.activate({"recall": {"action": "deactivate"}})
        self.assertEqual(self.read(), {"1": SCENE_ID, "3": SCENE_ID})

    def test_replay_thread_start_failure_is_reported_and_does_not_persist_stale_intent(self):
        self.play()
        with patch.object(QueuedThread, "start", side_effect=RuntimeError("synthetic")):
            self.assertEqual(self.controller.replay(), [])
        self.assertEqual(self.read(), {})
        self.assertEqual(self.controller.snapshot()["last_error"], "RuntimeError")
        self.assertEqual(self.controller.snapshot()["skipped_lights"], 3)

    def assert_huge_scene_value_is_skipped(self, field):
        self.play()
        other = copy.copy(self.scene)
        other.id_v2 = OTHER_ID
        other.palette = copy.deepcopy(self.scene.palette)
        other.lights = [weakref.ref(self.lights["2"])]
        other.lightstates = {self.lights["2"]: {"xy": [0.4, 0.3]}}
        self.scenes["other"] = other
        other.activate({"recall": {"action": "dynamic_palette"}})
        if field == "speed":
            self.scene.speed = 10**400
        elif field == "brightness":
            self.scene.palette["color"][0]["dimming"] = {"brightness": 10**400}
        else:
            self.scene.palette["color"][0]["color"]["xy"][field] = 10**400
        cold = ScenePlayIntent(self.path, {"1", "2", "3"}, self.guard,
                               self.lights, self.scenes)
        cold.scene_module = self.module
        QueuedThread.queued = []
        self.assertEqual(cold.replay(), ["2"])
        self.assertEqual(self.read(), {"2": OTHER_ID})
        self.assertEqual(cold.snapshot()["skipped_lights"], 2)
        self.assertEqual(len(QueuedThread.queued), 1)
        QueuedThread.queued[0].run()
        self.assertEqual([slot for slot, _ in self.calls], ["2"])

    def test_replay_skips_huge_integer_speed_and_continues_valid_scene(self):
        self.assert_huge_scene_value_is_skipped("speed")

    def test_replay_skips_huge_integer_brightness_and_continues_valid_scene(self):
        self.assert_huge_scene_value_is_skipped("brightness")

    def test_replay_skips_huge_integer_xy_and_continues_valid_scene(self):
        self.assert_huge_scene_value_is_skipped("x")

    def test_explicit_play_rejects_huge_integer_values_without_bookkeeping_error(self):
        palette = copy.deepcopy(self.scene.palette)
        for field in ("speed", "brightness", "x", "y", "mirek"):
            with self.subTest(field=field):
                self.scene.speed = 0.5
                self.scene.palette = copy.deepcopy(palette)
                self.play()
                if field == "speed":
                    self.scene.speed = 10**400
                elif field == "brightness":
                    self.scene.palette["color"][0]["dimming"] = {"brightness": 10**400}
                elif field == "mirek":
                    self.scene.palette["color"][0]["color_temperature"] = {"mirek": 10**400}
                else:
                    self.scene.palette["color"][0]["color"]["xy"][field] = 10**400
                self.play()
                self.assertEqual(self.read(), {})


@unittest.skipUnless(Path("/opt/hue-emulator/HueObjects/Scene.py").exists(),
                     "Requires the pinned public diyHue image")
class UpstreamScenePlayIntentTests(unittest.TestCase):
    def test_actual_scene_and_dynamic_worker_cold_recovery_with_manual_and_static_cancel(self):
        self.addCleanup(gc.collect)
        import logging
        sys.path.insert(0, "/opt/hue-emulator")
        logging.disable(logging.CRITICAL)
        from HueObjects import Light, Scene
        original_activate, original_thread = Scene.Scene.activate, Scene.Thread
        original_state, original_sleep = Light.Light._setV1State, Light.sleep
        self.addCleanup(setattr, Scene.Scene, "activate", original_activate)
        self.addCleanup(setattr, Scene, "Thread", original_thread)
        self.addCleanup(setattr, Light.Light, "_setV1State", original_state)
        self.addCleanup(setattr, Light, "sleep", original_sleep)
        self.addCleanup(lambda: QueuedThread.queued.clear())
        guard, emitted = PlayGuard(), []
        lights = {slot: Light.Light({"id_v1": slot, "name": "fixture-" + slot,
                                     "modelid": "LCT015", "protocol": "dummy"})
                  for slot in ("1", "2", "3", "4", "5", "6")}
        for lamp in lights.values():
            lamp.state["on"] = False

        def forward(lamp, values, advertise=True):
            if guard.worker_generation(lamp) is None:
                guard.cancel(lamp)
            emitted.append((lamp.id_v1, copy.deepcopy(values)))
            return original_state(lamp, values, advertise)

        Light.Light._setV1State = forward
        Light.sleep = lambda _: guard.cancel(lights[guard.local.context[0]])
        Scene.Thread = guard.thread_factory(QueuedThread, Light.Light.dynamicScenePlay,
                                             index_selector=desk_palette_index)
        scene = Scene.Scene({"id_v1": "fixture-scene", "id_v2": SCENE_ID,
                             "name": "fixture", "owner": SimpleNamespace(username="fixture"),
                             "lights": [weakref.ref(lamp) for lamp in lights.values()],
                             "palette": {"color": [{"color": {"xy": {"x": 0.2, "y": 0.3}}},
                                                    {"color": {"xy": {"x": 0.4, "y": 0.3}}}]}})
        for lamp in lights.values():
            scene.lightstates[lamp] = {"on": False, "xy": [0.2, 0.3]}
        before = copy.deepcopy(scene.palette)
        QueuedThread.queued = []
        with tempfile.TemporaryDirectory(prefix="tmp_upstream-play-intent-") as directory:
            path = Path(directory) / "intent.json"
            controller = install_scene_play_intent(Scene, Light.Light, guard, lights,
                                                   {"fixture": scene}, set(lights), path)
            scene.activate({"recall": {"action": "dynamic_palette"}})
            for thread in QueuedThread.queued:
                thread.run()
            self.assertEqual(len(controller.intents), 6)
            lights["2"].setV2State({"color": {"xy": {"x": 0.6, "y": 0.3}}})
            cold = ScenePlayIntent(path, set(lights), guard, lights, {"fixture": scene})
            cold.scene_module = Scene
            QueuedThread.queued = []
            emitted.clear()
            self.assertEqual(cold.replay(), ["1", "3", "4", "5", "6"])
            for thread in QueuedThread.queued:
                thread.run()
            self.assertEqual({slot for slot, _ in emitted}, {"1", "3", "4", "5", "6"})
            self.assertTrue(all("on" not in state for _, state in emitted))
            self.assertTrue(all(lamp.state["on"] is False for lamp in lights.values()))
            self.assertEqual(scene.palette, before)
            # A normal static recall must clear every affected persisted member.
            scene.activate({"recall": {"action": "active"}})
            self.assertEqual(json.loads(path.read_text())["lights"], {})


if __name__ == "__main__":
    unittest.main()
