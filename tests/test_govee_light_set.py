"""Any number of Govee lights by registry id, with model tiers (#7, stage 3a)."""

import copy
import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge"))
import live_adapter as adapter  # noqa: E402
from desk_layout import LayoutStore  # noqa: E402
from entertainment_stream import CHANNELS, VideoLease  # noqa: E402
from govee_segments import razer_colors  # noqa: E402
from test_desk_layout import layout  # noqa: E402
from test_live_adapter import LEFT, RIGHT, command, targets  # noqa: E402
from test_setup_studio import studio  # noqa: E402
from test_video_output import FakeProxy, FakeRelay  # noqa: E402
from video_output import VideoOutput  # noqa: E402

THIRD_ID = "device-" + "c" * 32
THIRD = "CC:CC:CC:CC:CC:CC:CC:CC"
FOURTH_ID = "device-" + "d" * 32
FOURTH = "DD:DD:DD:DD:DD:DD:DD:DD"


def lights(profile=True, third_model="H6008"):
    """Two H6076 plus one light of another model, as a registry would give them."""
    result = targets()
    result[0]["sku"] = "H6076"
    result[1]["model"] = "H6076"
    result.append({"light": THIRD_ID, "device": THIRD, "ip": "192.168.10.52",
                   "brightness_scale": 1, "model": third_model})
    adapter.prepare_targets(result, {"lights": {}} if profile else None)
    return result


class PrepareTargetsTests(unittest.TestCase):
    def test_segmented_models_get_segments_and_unknown_models_do_not(self):
        configured = lights()
        self.assertEqual([item["segment_count"] for item in configured], [7, 7, 0])
        self.assertEqual([item.get("max_gradient_packets_per_second") for item in configured],
                         [2, 2, None])

    def test_without_a_sync_profile_nothing_is_enabled(self):
        with patch.object(adapter, "GRADIENT_MODE", False):
            self.assertEqual([item["segment_count"] for item in lights(profile=False)],
                             [0, 0, 0])

    def test_the_gradient_mode_enables_segmented_models_only(self):
        with patch.object(adapter, "GRADIENT_MODE", True):
            self.assertEqual([item["segment_count"] for item in lights(profile=False)],
                             [7, 7, 0])

    def test_a_basic_tier_override_wins_over_the_model_and_a_stored_count(self):
        selected = targets()
        selected[0].update(sku="H6076", tier="basic", segment_count=7,
                           max_gradient_packets_per_second=2)
        selected[1].update(sku="H6076")
        adapter.prepare_targets(selected, {"lights": {}})
        self.assertEqual([item["segment_count"] for item in selected], [0, 7])
        controller = adapter.GoveeController(selected)
        self.assertEqual([item["tier"] for item in controller.snapshot()["targets"]],
                         ["basic", "segmented"])

    def test_a_model_outside_the_table_drops_a_stored_segment_count(self):
        selected = targets()
        selected[0].update(segment_count=7, model="H6008")
        selected[1].update(model="H6008")
        adapter.prepare_targets(selected, {"lights": {}})
        self.assertEqual([item["segment_count"] for item in selected], [0, 0])
        controller = adapter.GoveeController(selected)
        self.assertEqual([item["tier"] for item in controller.snapshot()["targets"]],
                         ["basic", "basic"])

    def test_a_new_light_without_a_model_is_basic_even_in_the_gradient_mode(self):
        with patch.object(adapter, "GRADIENT_MODE", True):
            selected = targets()
            selected.append({"light": THIRD_ID, "device": THIRD, "ip": "192.168.10.52",
                             "brightness_scale": 1})
            adapter.prepare_targets(selected, {"lights": {}})
            self.assertEqual([item["segment_count"] for item in selected], [7, 7, 0])
            controller = adapter.GoveeController(selected)
            self.assertEqual([item["tier"] for item in controller.snapshot()["targets"]],
                             ["segmented", "segmented", "basic"])


    def test_migrated_lights_without_a_model_keep_the_gradient_mode_segments(self):
        # The registry migrated from the live targets file carries no model: the
        # NAS lamps must stay segmented in the gradient mode, as before tiers.
        for gradient, expected in ((True, [7, 7]), (False, [0, 0])):
            with self.subTest(gradient=gradient), patch.object(adapter, "GRADIENT_MODE",
                                                               gradient):
                selected = targets()
                adapter.prepare_targets(selected, {"lights": {}})
                self.assertEqual([item["segment_count"] for item in selected], expected)
                controller = adapter.GoveeController(selected)
                self.assertEqual([item["tier"] for item in controller.snapshot()["targets"]],
                                 ["segmented", "segmented"])
                self.assertEqual(controller.razer_idle_seconds("govee-left"), 45)


class ThreeLightControllerTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.sent = []
        self.controller = adapter.GoveeController(
            lights(), sender=self.send, clock=lambda: self.now, wall_clock=lambda: 1000.0)
        self.controller.update_statuses({
            key: ({"onOff": 1, "brightness": 65}, 0.0) for key in (LEFT, RIGHT, THIRD)})

    def send(self, device, name, data):
        self.sent.append((device["light"], name, copy.deepcopy(data)))
        return True

    def names(self, light):
        return [name for item, name, _ in self.sent if item == light]

    def test_every_light_has_its_own_state_and_unknown_ones_are_refused(self):
        self.assertEqual(set(self.controller.by_light), {"govee-left", "govee-right", THIRD_ID})
        for light in self.controller.by_light:
            self.assertTrue(self.controller.submit(command(light=light)))
        with self.assertRaises(ValueError):
            self.controller.submit(command(light="govee-middle"))
        with self.assertRaises(ValueError):
            self.controller.submit(command(light="left"))
        self.assertEqual(set(self.controller.jobs), set(self.controller.by_light))
        self.assertEqual(set(self.controller.power_observations()), set(self.controller.by_light))

    def test_a_basic_light_gets_color_and_brightness_but_never_segments_or_dreamview(self):
        scene = {"on": True, "bri": 254, "colormode": "rgb",
                 "segments": [[120, 0, 0], [0, 0, 120]]}
        self.controller.submit(command(light="govee-left", transitiontime=0, state=scene))
        with self.assertRaises(ValueError):
            # A zoned state needs the explicit segment profile.
            self.controller.submit(command(light=THIRD_ID, transitiontime=0, state=scene))
        self.controller.submit(command(light=THIRD_ID, transitiontime=0))
        self.controller.render()
        self.assertEqual(self.names("govee-left")[0], "ptReal")
        self.assertEqual(sorted(self.names(THIRD_ID)), ["brightness", "colorwc"])
        self.sent.clear()
        self.controller.set_streaming(True)
        self.now = 1.0
        self.controller.update_statuses({
            key: ({"onOff": 1, "brightness": 65}, 1.0) for key in (LEFT, RIGHT, THIRD)})
        for sequence, light in enumerate(("govee-left", THIRD_ID), start=10):
            self.controller.submit(command(light=light, sequence=sequence, transitiontime=0,
                                           state={**scene, "segments": [[0, 90, 0]]}
                                           if light == "govee-left" else command()["state"]))
        self.controller.render()
        self.assertIn("razer", self.names("govee-left"))
        self.assertNotIn("razer", self.names(THIRD_ID))
        self.assertNotIn("ptReal", self.names(THIRD_ID))
        self.assertNotIn(THIRD_ID, self.controller.razer)
        self.assertEqual(self.controller.snapshot()["dreamview_lights"], ["govee-left"])

    def test_timing_and_stream_rate_come_from_the_model_table(self):
        self.assertEqual(self.controller.stream_rates, {
            "govee-left": 30, "govee-right": 30, THIRD_ID: adapter.STREAM_FRAMES_PER_SECOND})
        self.assertEqual(self.controller.razer_idle_seconds("govee-left"), 45)
        self.assertEqual(self.controller.razer_lost_seconds("govee-left"), 5)
        override = lights()
        override[0]["max_stream_frames_per_second"] = 20
        self.assertEqual(adapter.GoveeController(override).stream_rates["govee-left"], 20)

    def test_a_light_can_leave_without_disturbing_the_others(self):
        for light in self.controller.by_light:
            self.controller.submit(command(light=light, transitiontime=0))
        self.controller.clear_light(THIRD_ID)
        self.assertEqual(set(self.controller.jobs), {"govee-left", "govee-right"})

    def test_snapshot_reports_every_target_by_light(self):
        snapshot = self.controller.snapshot()
        self.assertEqual([item["light"] for item in snapshot["targets"]],
                         ["govee-left", "govee-right", THIRD_ID])
        self.assertEqual([item["tier"] for item in snapshot["targets"]],
                         ["segmented", "segmented", "basic"])
        self.assertEqual(set(snapshot["skipped_state"]), set(self.controller.by_light))


def profile_for(*ids, samples=None):
    samples = samples or [None, None, (0, 1), (0, 1), (0, 1), (1, 1), (1, 1)]
    return {"signature": "s", "basis": "screen-perimeter-v1", "enabled_roles": [],
            "native_roles": [], "plays": {},
            "lights": {light: {"samples": list(samples), "gains": [1] * len(samples)}
                       for light in ids}}


class Layout:
    def __init__(self, profile):
        self.profile = profile

    def enabled(self, light, fallback=True):
        return light in self.profile["lights"]

    present = enabled

    def scale(self, light, fallback):
        return fallback

    def sync_profile(self):
        return copy.deepcopy(self.profile)


class ProfileRelay(FakeRelay):
    def start(self, profile=None):
        return super().start()


class SyncTiersTests(unittest.TestCase):
    def build(self, selected, profile):
        self.sent = []
        controller = adapter.GoveeController(
            selected, sender=lambda device, name, data: self.sent.append(
                (device["light"], name, copy.deepcopy(data))) or True,
            clock=lambda: 10, wall_clock=lambda: 1000, layout_store=Layout(profile))
        controller.update_statuses({
            adapter.lan.device_key(item): ({"onOff": 1, "brightness": 65}, 10)
            for item in selected})
        self.lease = VideoLease(clock=lambda: 10, wall_clock=lambda: 1000)
        self.output = VideoOutput(controller, ProfileRelay(), [FakeProxy() for _ in range(4)],
                                  lease=self.lease, clock=lambda: 10)
        return controller

    def frame(self):
        rows = [[index, 0, 0, 0] for index in sorted(CHANNELS)]
        rows[4], rows[8] = [4, 65535, 0, 0], [8, 0, 0, 65535]
        self.lease.submit({"action": "frame", "session": "tiers", "sequence": 1,
                           "issued_at": 1000, "colors": rows})

    def test_a_basic_light_follows_the_representative_color_of_its_placement(self):
        selected = lights()
        controller = self.build(selected, profile_for("govee-left", "govee-right", THIRD_ID))
        self.frame()
        self.assertTrue(self.output.process_once())
        third = controller.target(THIRD_ID)
        self.assertNotIn("segments", third)
        # The projected center of its visible zones: the sample at channel 4 is red.
        self.assertEqual(third["rgb"], (255, 0, 0))
        self.assertIn("segments", controller.target("govee-left"))
        names = [name for light, name, _ in self.sent if light == THIRD_ID]
        self.assertEqual(sorted(names), ["brightness", "colorwc"])

    def test_a_forced_basic_h6076_is_a_single_color_in_sync(self):
        selected = targets()
        selected[0].update(sku="H6076", tier="basic")
        selected[1].update(sku="H6076")
        adapter.prepare_targets(selected, {"lights": {}})
        controller = self.build(selected, profile_for("govee-left", "govee-right"))
        self.frame()
        self.assertTrue(self.output.process_once())
        forced = controller.target("govee-left")
        self.assertNotIn("segments", forced)
        self.assertEqual(forced["rgb"], (255, 0, 0))
        names = [name for light, name, _ in self.sent if light == "govee-left"]
        self.assertNotIn("razer", names)
        self.assertNotIn("ptReal", names)
        self.assertNotIn("govee-left", controller.razer)
        segmented = [razer_colors(data) for light, name, data in self.sent
                     if light == "govee-right" and name == "razer"]
        self.assertTrue(segmented)

    def test_lights_outside_the_composition_are_cleared_and_skipped(self):
        selected = lights()
        controller = self.build(selected, profile_for("govee-left", "govee-right"))
        # A scene command for a light outside the composition is not kept either.
        self.assertTrue(controller.submit(command(light=THIRD_ID, transitiontime=0)))
        self.assertNotIn(THIRD_ID, controller.jobs)
        self.frame()
        self.assertTrue(self.output.process_once())
        self.assertNotIn(THIRD_ID, controller.jobs)
        self.assertFalse([1 for light, _, _ in self.sent if light == THIRD_ID])

    def test_without_a_profile_only_the_migrated_lamps_have_capture_channels(self):
        selected = lights()
        controller = self.build(selected, profile_for())
        controller.layout_store.sync_profile = lambda: None
        controller.layout_store.enabled = lambda light, fallback=True: True
        self.frame()
        self.assertTrue(self.output.process_once())
        self.assertEqual(set(controller.jobs), {"govee-left", "govee-right"})


class CommandRouteTests(unittest.TestCase):
    def test_an_unknown_light_is_rejected_with_400_and_nothing_is_sent(self):
        sent = []
        controller = adapter.GoveeController(
            lights(), sender=lambda *args: sent.append(args) or True,
            clock=lambda: 0.0, wall_clock=lambda: 1000.0)
        server = ThreadingHTTPServer(("127.0.0.1", 0), adapter.handler_for(controller, "t"))
        worker = threading.Thread(target=server.serve_forever)
        worker.start()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

        def post(body):
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/command", data=json.dumps(body).encode(),
                headers={"X-Adapter-Token": "t"}, method="POST")
            try:
                with opener.open(request) as response:
                    return response.status, json.load(response)
            except urllib.error.HTTPError as error:
                with error:
                    return error.code, json.load(error)

        try:
            self.assertEqual(post(command(light=THIRD_ID)), (200, {"accepted": True}))
            for light in ("device-" + "e" * 32, "left", "govee-middle", 5):
                self.assertEqual(post(command(light=light, sequence=2))[0], 400)
            # The old payload key is gone.
            old = command()
            old["side"] = old.pop("light")
            self.assertEqual(post(old)[0], 400)
            controller.render()
            self.assertEqual(sent, [])
            self.assertEqual(set(controller.jobs), {THIRD_ID})
        finally:
            server.shutdown()
            server.server_close()
            worker.join()


class LayoutKeyTests(unittest.TestCase):
    """Rows are addressed by the light id, the native Hue rows included."""

    def store(self, folder):
        data = layout()
        data["version"] = 5
        data["devices"].append({
            "id": THIRD_ID, "scale": 80, "sceneScale": 70, "orientation": "horizontal",
            "positionCm": {"x": 100, "height": 60}, "name": "Third", "model": "H6008",
            "shape": "bar", "zones": 1})
        store = LayoutStore(Path(folder) / "layout.json")
        store.save(data)
        return store

    def test_scale_membership_and_calibration_follow_the_light_id(self):
        with TemporaryDirectory(prefix="tmp_light_keys_") as folder:
            store = self.store(folder)
            self.assertEqual(store.scale(THIRD_ID, 1), 0.8)
            self.assertEqual(store.scene_scale(THIRD_ID, 1), 0.7)
            self.assertEqual(store.scale("govee-left", 1), 0.65)
            self.assertEqual(store.scale("play-center", 1), 0.5)
            self.assertTrue(store.enabled(THIRD_ID))
            self.assertTrue(store.present("govee-right"))
            self.assertEqual(store.calibration(THIRD_ID), {"warmth": 0, "tint": 0, "lowLight": 0})
            # The old side and Hue role names no longer address a row.
            self.assertEqual(store.scale("left", 1), 1)
            self.assertEqual(store.scale("hue_center", 1), 1)
            self.assertEqual(store.calibration("left"), {"warmth": 0, "tint": 0, "lowLight": 0})
            self.assertEqual(store.names(), {})
            self.assertEqual(store.scene_fade()["govee-left"], [1] * 7)
            self.assertNotIn("left", store.scene_fade())

    def test_controller_uses_the_row_of_each_light(self):
        with TemporaryDirectory(prefix="tmp_light_keys_") as folder:
            controller = adapter.GoveeController(
                lights(), layout_store=self.store(folder), clock=lambda: 0.0,
                wall_clock=lambda: 1000.0)
            controller.update_statuses({THIRD: ({"onOff": 1, "brightness": 80}, 0.0)})
            controller.submit(command(light=THIRD_ID, transitiontime=0))
            controller.submit(command(transitiontime=0))
            # Full brightness times the row's own correction.
            self.assertEqual(controller.effective_target(THIRD_ID)["brightness"], 80)
            self.assertEqual(controller.effective_target("govee-left")["brightness"], 65)


class StudioStatusTests(unittest.TestCase):
    def status(self, tier):
        return {
            "targets": [{"light": "govee-left", "tier": tier, "status_age": 0.1,
                         "native": {"identity_verified": True, "onOff": 1, "brightness": 50}}],
            "video": {"active": False}, "hue": {},
            "preview": {"version": 1, "mode": "scene", "lamps": {"govee-left": {
                "online": True, "on": True, "source": "scene-output", "stale": False,
                "brightness": 50, "colors": [[255, 0, 0]] * 7}}},
        }

    def test_a_basic_tier_lamp_is_one_zone_and_a_segmented_one_has_seven(self):
        basic = studio.project_status(self.status("basic"), source_active=False)
        segmented = studio.project_status(self.status("segmented"), source_active=False)
        self.assertEqual(len(basic["lamps"]["govee-left"]["colors"]), 1)
        self.assertEqual(len(segmented["lamps"]["govee-left"]["colors"]), 7)

    def test_an_enrolled_light_joins_the_projection_and_unknown_ids_stay_out(self):
        data = self.status("segmented")
        data["targets"].append({"light": THIRD_ID, "status_age": 0.1, "native": {"onOff": 1}})
        data["targets"].append({"light": "third-govee", "status_age": 0.1,
                                "native": {"onOff": 1}})
        lamps = studio.project_status(data, source_active=False)["lamps"]
        self.assertEqual(len(lamps), 7)
        self.assertIn(THIRD_ID, lamps)


class StartupTests(unittest.TestCase):
    def test_the_adapter_starts_with_three_registry_lights_and_keeps_the_extra_one(self):
        from registry_files import write_registry

        class BeforeServer(Exception):
            pass

        captured = []
        constructor = adapter.GoveeController

        def capture(*args, **kwargs):
            result = constructor(*args, **kwargs)
            captured.extend(copy.deepcopy(result.targets))
            return result

        with TemporaryDirectory(prefix="tmp_three_lights_") as directory:
            root = Path(directory)
            write_registry(root, targets())
            data = json.loads((root / "lights.json").read_text())
            data["lights"].append({
                "id": THIRD_ID, "provider": "govee",
                "virtual": {"id_v1": "7", "seed": "Device1"},
                "govee": {"device": THIRD, "ip": "192.168.10.52", "model": "H6008"}})
            (root / "lights.json").write_text(json.dumps(data))
            (root / "token").write_text("a" * 48)
            with (patch.object(adapter, "GoveeController", side_effect=capture),
                  patch.object(adapter, "ThreadingHTTPServer", side_effect=BeforeServer),
                  self.assertRaises(BeforeServer)):
                adapter.main(["--registry", str(root / "lights.json"),
                              "--token-file", str(root / "token")])
        self.assertEqual([item["light"] for item in captured],
                         ["govee-left", "govee-right", THIRD_ID])
        # Without a layout only the gradient mode enables the segments of the migrated
        # lamps, which carry no model; the extra light of another model stays basic.
        legacy = 7 if adapter.GRADIENT_MODE else 0
        self.assertEqual([item["segment_count"] for item in captured], [legacy, legacy, 0])


if __name__ == "__main__":
    unittest.main()
