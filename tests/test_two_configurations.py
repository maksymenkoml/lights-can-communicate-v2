"""With-Sync and without-Sync (scene) configurations of layout version 5."""
import copy
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge"))
from desk_layout import LayoutStore, validate_layout  # noqa: E402
from hue_strip_proxy import HueStripProxy  # noqa: E402
from live_adapter import GoveeController  # noqa: E402
from sync_projection import (  # noqa: E402
    project_govee_frame,
    project_native_frame,
    scene_fade,
    sync_profile,
)  # noqa: E402
from sync_zone import zone_points  # noqa: E402
from test_floor_zone_contract import custom_zone  # noqa: E402
from test_hue_strip_proxy import FakeHue  # noqa: E402
from test_hue_strip_proxy import command as hue_command  # noqa: E402
from test_live_adapter import LEFT, RIGHT, command, targets  # noqa: E402
from test_native_profile import PLAY_IDS  # noqa: E402
from test_sync_projection import frame, layout  # noqa: E402


def v5(**changes):
    data = layout()
    data["version"] = 5
    for row in data["devices"]:
        row["scale"] = 65 if row["id"].startswith("govee") else 100
    return data | changes


def row(data, key):
    return next(item for item in data["devices"] if item["id"] == key)


class FakeClock:
    value = 0

    def __call__(self):
        return self.value


class StoreCase(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory(prefix="tmp_two_configurations_")
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "desk-layout.json"
        self.store = LayoutStore(self.path)

    def save(self, data):
        self.store.save(data)


class LayoutFieldTests(StoreCase):
    def test_older_versions_migrate_and_defaults_are_not_stored(self):
        for version in (3, 4):
            data = v5(version=version)
            with self.subTest(version=version):
                result = validate_layout(data)
                self.assertEqual(result["version"], 5)
                for key in ("sceneZone", "syncFalloff", "sceneFalloff"):
                    self.assertNotIn(key, result)
                self.assertTrue(all("sceneScale" not in item for item in result["devices"]))

    def test_new_fields_validate_roundtrip_and_store_only_non_defaults(self):
        data = v5(sceneZone=custom_zone(), syncFalloff=False, sceneFalloff=True)
        row(data, "play-left")["sceneScale"] = 30
        result = validate_layout(data)
        self.assertEqual(result["sceneZone"], custom_zone())
        self.assertIs(result["syncFalloff"], False)
        self.assertIs(result["sceneFalloff"], True)
        self.assertEqual(row(result, "play-left")["sceneScale"], 30)
        self.assertEqual(validate_layout(copy.deepcopy(result)), result)
        defaults = validate_layout(v5(syncFalloff=True, sceneFalloff=False, sceneZone=None))
        for key in ("sceneZone", "syncFalloff", "sceneFalloff"):
            self.assertNotIn(key, defaults)

    def test_invalid_new_fields_are_refused(self):
        for key, value in (("syncFalloff", 1), ("sceneFalloff", "yes"), ("syncFalloff", None),
                           ("sceneZone", {"version": 1, "points": []})):
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                validate_layout(v5(**{key: value}))
        for value in (-1, 101, "50", True, float("nan")):
            data = v5()
            row(data, "govee-left")["sceneScale"] = value
            with self.subTest(sceneScale=value), self.assertRaises(ValueError):
                validate_layout(data)

    def test_scene_scale_falls_back_to_scale_and_membership_ignores_sync_role(self):
        data = v5()
        row(data, "govee-left")["sceneScale"] = 40
        row(data, "play-left")["syncRole"] = "own"
        row(data, "play-right")["enabled"] = False
        self.save(data)
        self.assertEqual(self.store.scene_scale("left", 1), 0.4)
        self.assertEqual(self.store.scale("left", 1), 0.65)
        self.assertEqual(self.store.scene_scale("right", 1), 0.65)
        self.assertEqual(self.store.scene_scale("unknown", 0.7), 0.7)
        self.assertFalse(self.store.enabled("hue_left"))
        self.assertTrue(self.store.present("hue_left"))
        self.assertTrue(self.store.present("play-left"))
        self.assertFalse(self.store.present("hue_right"))
        self.assertTrue(self.store.present("strip"))

    def test_corrupt_saved_layout_removes_scene_membership_like_sync_membership(self):
        self.path.write_text("{")
        self.assertFalse(self.store.present("left"))
        self.assertFalse(self.store.enabled("left"))
        self.assertTrue(LayoutStore(self.path.with_name("missing.json")).present("left"))

    def test_zone_points_resolve_the_scene_zone_independently(self):
        data = v5(sceneZone=custom_zone())
        self.assertEqual(zone_points(data, "sceneZone")[0]["x"],
                         100 + custom_zone()["points"][0]["u"] * 80)
        self.assertNotEqual(zone_points(data, "sceneZone"), zone_points(data))
        self.assertEqual(zone_points(v5(), "sceneZone"), zone_points(v5()))


class FadeProjectionTests(unittest.TestCase):
    def test_sync_fade_off_gives_full_gain_and_centred_samples(self):
        faded = sync_profile(v5())
        flat = sync_profile(v5(syncFalloff=False))
        self.assertIn(None, faded["govee"]["left"]["samples"])
        for side in ("left", "right"):
            row_ = flat["govee"][side]
            self.assertEqual(row_["gains"], [1] * 7)
            self.assertNotIn(None, row_["samples"])
            self.assertEqual(row_["visible"], [True] * 7)
            self.assertEqual(row_["fade_cm"], 0)
        self.assertEqual(flat["strip_gains"], [1] * 9)
        self.assertEqual(set(flat["play_gains"].values()), {1})
        self.assertNotEqual(flat["signature"], faded["signature"])
        self.assertEqual(flat, sync_profile(v5(syncFalloff=False)))
        colors = project_govee_frame(frame(), "left", flat)
        self.assertNotIn((0, 0, 0), colors)

    def test_sync_fade_off_reaches_the_native_projection_used_by_the_relay(self):
        raw = {channel: (60000, 30000, 15000) for channel in range(18)}
        faded = project_native_frame(raw, sync_profile(v5()))
        flat = project_native_frame(raw, sync_profile(v5(syncFalloff=False)))
        self.assertEqual(faded[12], (0, 0, 0))
        self.assertEqual(flat[12], raw[12])

    def test_scene_fade_follows_the_scene_switch_and_zone_not_the_sync_ones(self):
        self.assertEqual(scene_fade(v5()), {"left": [1] * 7, "right": [1] * 7, "strip": 1,
                                            "hue_left": 1, "hue_center": 1, "hue_right": 1})
        on = scene_fade(v5(sceneFalloff=True))
        self.assertEqual(on["left"][:3], [0, 0, 0])
        self.assertTrue(0 < on["left"][3] < on["left"][4] < 1)
        self.assertEqual(on["left"][5:], [1, 1])
        self.assertEqual(on["hue_center"], 0)
        self.assertEqual(on["strip"], 1)
        low = v5(sceneFalloff=True)
        row(low, "hue-monitor")["positionCm"]["height"] = 90
        self.assertTrue(0 < scene_fade(low)["strip"] < 1)
        # The Sync profile is not affected by the scene switch, nor the scene by Sync's.
        self.assertEqual(sync_profile(v5(sceneFalloff=True))["govee"],
                         sync_profile(v5())["govee"])
        self.assertEqual(scene_fade(v5(sceneFalloff=True, syncFalloff=False)), on)
        wide = scene_fade(v5(sceneFalloff=True, sceneZone=custom_zone()))
        self.assertNotEqual(wide, on)

    def test_scene_strip_gain_is_the_mean_of_the_nine_anchor_gains(self):
        data = v5(sceneFalloff=True, syncFalloff=True)
        row(data, "hue-monitor")["positionCm"]["height"] = 90
        mean = sum(sync_profile(data)["strip_gains"]) / 9
        self.assertAlmostEqual(scene_fade(data)["strip"], mean)

    def test_store_scene_fade_is_cached_per_file_version_and_none_without_a_layout(self):
        with TemporaryDirectory(prefix="tmp_two_configurations_") as folder:
            store = LayoutStore(Path(folder) / "desk-layout.json")
            self.assertIsNone(store.scene_fade())
            store.save(v5(sceneFalloff=True))
            first = store.scene_fade()
            self.assertEqual(first["hue_center"], 0)
            first["hue_center"] = 9
            self.assertEqual(store.scene_fade()["hue_center"], 0)
            store.save(v5())
            self.assertEqual(store.scene_fade()["hue_center"], 1)


class ControllerTests(StoreCase):
    def make(self, configuration, segmented=True):
        items = targets()
        if segmented:
            items[0]["segment_count"] = 7
        self.clock, self.sent = FakeClock(), []
        controller = GoveeController(
            items, sender=lambda device, name, data: self.sent.append((name, data)) or True,
            clock=self.clock, wall_clock=lambda: 1000, layout_store=self.store,
            power_owner=None, configuration=configuration)
        controller.update_statuses({LEFT: ({"onOff": 1}, 0), RIGHT: ({"onOff": 1}, 0)})
        return controller

    def test_unknown_configuration_is_refused(self):
        with self.assertRaises(ValueError):
            GoveeController(targets(), configuration="both")

    def test_scene_uses_scene_scale_and_sync_keeps_scale(self):
        data = v5()
        row(data, "govee-left")["sceneScale"] = 40
        self.save(data)
        for configuration, brightness in (("scene", 40), ("sync", 65)):
            controller = self.make(configuration)
            controller.submit(command(transitiontime=0))
            with self.subTest(configuration=configuration):
                self.assertEqual(controller.effective_target("left")["brightness"], brightness)

    def test_scene_scale_change_resends_only_brightness(self):
        data = v5()
        self.save(data)
        controller = self.make("scene", segmented=False)
        controller.submit(command(transitiontime=0))
        controller.render()
        self.sent.clear()
        row(data, "govee-left")["sceneScale"] = 30
        self.save(data)
        controller.render()
        self.assertEqual(self.sent, [("brightness", {"value": 30})])

    def test_scene_fade_converts_colour_to_attenuated_segments_and_sync_does_not(self):
        self.save(v5(sceneFalloff=True))
        gains = scene_fade(v5(sceneFalloff=True))["left"]
        scene = self.make("scene")
        scene.submit(command(transitiontime=0))
        target = scene.effective_target("left")
        self.assertNotIn("rgb", target)
        self.assertEqual(len(target["segments"]), 7)
        base = scene.target("left")["rgb"]
        self.assertEqual(target["segments"][:3], [(0, 0, 0)] * 3)
        self.assertEqual(target["segments"][3], tuple(round(v * gains[3]) for v in base))
        self.assertEqual(target["segments"][6], base)
        self.assertEqual(scene.jobs["left"]["to"].get("rgb"), base)
        scene.render()
        self.assertEqual({name for name, _ in self.sent}, {"ptReal", "brightness"})
        sync = self.make("sync")
        sync.submit(command(transitiontime=0))
        self.assertNotIn("segments", sync.effective_target("left"))

    def test_scene_fade_attenuates_native_segments_and_kelvin_targets(self):
        self.save(v5(sceneFalloff=True))
        controller = self.make("scene")
        state = {"on": True, "bri": 254, "colormode": "ct", "ct": 250}
        controller.submit(command(state=state, transitiontime=0))
        target = controller.effective_target("left")
        self.assertNotIn("kelvin", target)
        self.assertEqual(target["segments"][:3], [(0, 0, 0)] * 3)
        segments = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (10, 20, 30)] + [(200, 100, 50)] * 3
        controller.submit(command(sequence=2, transitiontime=0,
                                  state={"on": True, "bri": 254, "segments": segments}))
        target = controller.effective_target("left")
        self.assertEqual(target["segments"][:3], [(0, 0, 0)] * 3)
        self.assertEqual(target["segments"][6], (200, 100, 50))

    def test_scene_fade_off_leaves_the_target_untouched(self):
        self.save(v5())
        controller = self.make("scene")
        controller.submit(command(transitiontime=0))
        target = controller.effective_target("left")
        self.assertIn("rgb", target)
        self.assertNotIn("segments", target)

    def test_device_without_segments_is_dimmed_by_the_mean_gain(self):
        self.save(v5(sceneFalloff=True))
        mean = sum(scene_fade(v5(sceneFalloff=True))["left"]) / 7
        controller = self.make("scene", segmented=False)
        controller.submit(command(transitiontime=0))
        target = controller.effective_target("left")
        self.assertIn("rgb", target)
        self.assertEqual(target["brightness"], max(1, round(65 * mean)))
        sync = self.make("sync", segmented=False)
        sync.submit(command(transitiontime=0))
        self.assertEqual(sync.effective_target("left")["brightness"], 65)

    def test_initial_native_brightness_is_not_scaled_twice(self):
        self.save(v5(sceneFalloff=True))
        mean = sum(scene_fade(v5(sceneFalloff=True))["left"]) / 7
        controller = self.make("scene", segmented=False)
        native = max(1, round(65 * mean))
        controller.update_statuses(
            {LEFT: ({"onOff": 1, "brightness": native, "color": {"r": 255, "g": 0, "b": 0}}, 0)})
        controller.submit(command(transitiontime=20))
        self.assertEqual(controller.effective_target("left")["brightness"], native)

    def test_own_light_lamp_belongs_to_scene_output_but_not_to_sync(self):
        data = v5()
        row(data, "govee-left")["syncRole"] = "own"
        self.save(data)
        scene, sync = self.make("scene"), self.make("sync")
        self.assertTrue(scene.included("left"))
        self.assertFalse(sync.included("left"))
        scene.submit(command(transitiontime=0))
        sync.submit(command(transitiontime=0))
        self.assertIn("left", scene.jobs)
        self.assertNotIn("left", sync.jobs)
        data["devices"][0]["enabled"] = False
        self.save(data)
        scene.render()
        self.assertFalse(scene.included("left"))
        self.assertNotIn("left", scene.jobs)


class HueProxyTests(StoreCase):
    def proxy(self, role="hue_center"):
        client = FakeHue()
        if role in PLAY_IDS:
            client.resource_id = PLAY_IDS[role]
            client.native["id"] = client.resource_id
        proxy = HueStripProxy(client, role=role, clock=lambda: 10, wall_clock=lambda: 1000,
                              layout_store=self.store)
        proxy.submit(hue_command(side=role, transitiontime=0, state={
            "on": True, "bri": 254, "colormode": "xy", "xy": [.2, .3],
            "_changed": ["on", "bri", "xy"]}))
        return proxy, client

    def test_scene_factor_is_scene_scale_times_fade_gain_and_toggles_with_one_restore(self):
        data = v5(sceneFalloff=True)
        row(data, "play-center")["positionCm"]["height"] = 80
        gain = scene_fade(data)["hue_center"]
        self.assertTrue(0 < gain < 1)
        row(data, "play-center")["scale"] = 50
        row(data, "play-center")["sceneScale"] = 80
        self.save(data)
        proxy, client = self.proxy()
        self.assertTrue(proxy.process_once())
        self.assertAlmostEqual(client.writes[-1]["dimming"]["brightness"], 100 * .8 * gain)
        self.assertFalse(proxy.process_once())
        self.assertEqual(len(client.writes), 1)
        data["sceneFalloff"] = False
        self.save(data)
        self.assertTrue(proxy.process_once())
        self.assertAlmostEqual(client.writes[-1]["dimming"]["brightness"], 80)
        self.assertFalse(proxy.process_once())
        self.assertEqual(len(client.writes), 2)
        # The Sync switch and scale do not reach the scene path.
        data["syncFalloff"] = False
        data["devices"][4]["scale"] = 10
        self.save(data)
        self.assertFalse(proxy.process_once())
        self.assertEqual(len(client.writes), 2)

    def test_deep_play_never_dims_below_the_floor(self):
        self.save(v5(sceneFalloff=True))
        proxy, client = self.proxy()
        self.assertTrue(proxy.process_once())
        self.assertEqual(client.writes[-1]["dimming"]["brightness"], 0.1)
        self.assertNotIn("on", client.writes[-1])

    def test_strip_uses_the_mean_anchor_gain(self):
        data = v5(sceneFalloff=True)
        row(data, "hue-monitor")["positionCm"]["height"] = 90
        self.save(data)
        proxy, client = self.proxy("strip")
        self.assertAlmostEqual(proxy.layout_factor(), scene_fade(data)["strip"])
        self.assertTrue(proxy.process_once())
        self.assertAlmostEqual(client.writes[-1]["dimming"]["brightness"],
                               max(0.1, 100 * scene_fade(data)["strip"]))

    def test_own_light_lamp_keeps_scene_membership_and_removal_excludes_it(self):
        data = v5()
        row(data, "play-center")["syncRole"] = "own"
        self.save(data)
        proxy, client = self.proxy()
        self.assertTrue(proxy.included())
        self.assertTrue(proxy.process_once())
        self.assertTrue(proxy.govee_allowed())
        row(data, "play-center")["enabled"] = False
        self.save(data)
        self.assertFalse(proxy.included())
        self.assertFalse(proxy.process_once())

    def test_layout_file_round_trips_through_json(self):
        data = v5(sceneFalloff=True, syncFalloff=False)
        self.save(data)
        stored = json.loads(self.path.read_text())
        self.assertEqual((stored["version"], stored["sceneFalloff"], stored["syncFalloff"]),
                         (5, True, False))


if __name__ == "__main__":
    unittest.main()
