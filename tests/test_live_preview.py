"""Synthetic output telemetry tests; no devices or network are used."""

import base64
import copy
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge"))
import live_adapter as adapter  # noqa: E402
import test_hue_entertainment_relay as relay_tests  # noqa: E402
import test_video_output as video_tests  # noqa: E402
from entertainment_stream import GRADIENT_MODE  # noqa: E402
from govee_segments import razer_data, razer_mode, segment_data  # noqa: E402
from hue_entertainment_relay import HueEntertainmentRelay  # noqa: E402
from hue_strip_proxy import HueStripProxy  # noqa: E402
from live_adapter import GoveeController, handler_for  # noqa: E402
from registry_files import write_registry  # noqa: E402
from test_entertainment_stream import envelope  # noqa: E402
from test_hue_strip_proxy import FakeHue, command  # noqa: E402
from test_live_adapter import LEFT, targets  # noqa: E402
from test_native_profile import PLAY_IDS, profile_data  # noqa: E402

from lights_can_communicate_v2_core.live_preview import OutputPreviewStore, build_preview  # noqa: E402
from lights_can_communicate_v2_core.native_profile import NativeHueProfile  # noqa: E402


def metadata():
    hue = {"healthy": True, "native_age": 0.1, "native_on": True, "native_mode": "normal"}
    return {
        "targets": [
            {
                "light": "govee-left",
                "status_age": 0.1,
                "native": {"identity_verified": True, "onOff": 1, "brightness": 65},
            }
        ],
        "hue": {
            light: copy.deepcopy(hue)
            for light in ("hue-monitor", "play-left", "play-center", "play-right")
        },
    }


class PreviewTests(unittest.TestCase):
    def setUp(self):
        self.now = 10.0
        self.store = OutputPreviewStore(clock=lambda: self.now)
        self.send = self.store.wrap_govee(lambda *_: True, "scene-output")
        self.device = targets()[0]

    def lamp(self, status=None, key="govee-left"):
        return build_preview(metadata() if status is None else status, self.store.snapshot(),
                             self.now)[
            "lamps"
        ][key]

    def test_dispatched_segments_and_brightness_are_combined_exactly_once(self):
        colors = [
            (255, 0, 0),
            (0, 255, 0),
            (0, 0, 255),
            (100, 80, 60),
            (0, 0, 0),
            (255, 255, 255),
            (10, 20, 30),
        ]
        self.send(self.device, "ptReal", segment_data(colors))
        self.send(self.device, "brightness", {"value": 65})
        self.assertEqual(
            self.lamp()["colors"],
            [
                [166, 0, 0],
                [0, 166, 0],
                [0, 0, 166],
                [65, 52, 39],
                [0, 0, 0],
                [166, 166, 166],
                [6, 13, 20],
            ],
        )
        self.send(self.device, "brightness", {"value": 20})
        self.assertEqual(self.lamp()["colors"][0], [51, 0, 0])
        self.assertEqual(self.lamp()["brightness"], 20)
        self.now += 500
        self.assertEqual(self.lamp()["colors"][0], [51, 0, 0])

    def test_dreamview_frames_show_like_segments_and_mode_switches_are_ignored(self):
        self.send(self.device, "brightness", {"value": 100})
        self.send(self.device, "razer", razer_mode(True))
        self.send(self.device, "razer", razer_data([(255, 0, 0)] * 6 + [(0, 0, 255)]))
        self.assertEqual(self.lamp()["colors"], [[255, 0, 0]] * 6 + [[0, 0, 255]])
        self.send(self.device, "razer", razer_mode(False))
        self.assertEqual(self.lamp()["colors"], [[255, 0, 0]] * 6 + [[0, 0, 255]])
        before = self.lamp()["colors"]
        self.send(self.device, "razer", {"pt": "invalid"})
        self.assertEqual(self.lamp()["colors"], before)

    def test_failed_sends_and_partial_dispatch_do_not_advance_unsent_fields(self):
        results = iter([True, True, True, False])
        send = self.store.wrap_govee(lambda *_: next(results), "scene-output")
        send(self.device, "colorwc", {"color": {"r": 255, "g": 0, "b": 0}, "colorTemInKelvin": 0})
        send(self.device, "brightness", {"value": 50})
        send(self.device, "colorwc", {"color": {"r": 0, "g": 0, "b": 255}, "colorTemInKelvin": 0})
        self.assertFalse(send(self.device, "brightness", {"value": 10}))
        self.assertEqual(self.lamp()["colors"], [[0, 0, 128]])
        self.assertEqual(self.lamp()["brightness"], 50)
        failing = self.store.wrap_govee(
            lambda *_: (_ for _ in ()).throw(OSError("test")), "sync-output"
        )
        with self.assertRaises(OSError):
            failing(self.device, "brightness", {"value": 1})
        self.assertEqual(self.lamp()["brightness"], 50)

    def test_invalid_packets_unknown_roles_and_power_reads_do_not_create_samples(self):
        for cmd, data in [
            ("scan", {}),
            ("devStatus", {}),
            ("turn", {"value": 0}),
            ("brightness", {"value": True}),
            ("colorwc", {"color": {"r": 300, "g": 0, "b": 0}}),
            ("ptReal", {"command": ["invalid"] * 7}),
        ]:
            self.send(self.device, cmd, data)
        self.send({"light": "unbound", "device": "secret"}, "brightness", {"value": 50})
        self.assertEqual(self.store.snapshot(), {})
        self.assertEqual(self.lamp()["colors"], [])

    def test_a_light_enrolled_later_is_previewed_by_its_registry_id(self):
        enrolled, hue = "device-" + "c" * 32, "device-" + "e" * 32
        self.send({"light": enrolled, "device": "secret"}, "colorwc",
                  {"color": {"r": 255, "g": 0, "b": 0}, "colorTemInKelvin": 0})
        self.send({"light": enrolled, "device": "secret"}, "brightness", {"value": 50})
        status = metadata()
        status["targets"].append({"light": enrolled, "status_age": 0.1, "native": {
            "identity_verified": True, "onOff": 1, "brightness": 50}})
        status["hue"][hue] = copy.deepcopy(status["hue"]["play-left"])
        lamps = build_preview(status, self.store.snapshot())["lamps"]
        self.assertEqual(list(lamps)[:6], ["govee-left", "govee-right", "hue-monitor",
                                           "play-left", "play-center", "play-right"])
        self.assertEqual(lamps[enrolled]["colors"], [[128, 0, 0]])
        self.assertEqual((lamps[hue]["online"], lamps[hue]["on"]), (True, True))
        # With a Sync profile, a later light is included only while its row is composed.
        status["composition"] = {"enabled_roles": ["left"], "lights": ["govee-left", hue]}
        lamps = build_preview(status, self.store.snapshot())["lamps"]
        self.assertEqual((lamps[enrolled]["included"], lamps[hue]["included"]), (False, True))
        self.assertFalse(lamps["govee-right"]["included"])
        # A registry light without any reading is still previewed, its state unknown.
        quiet_govee, quiet_hue = "device-" + "d" * 32, "device-" + "f" * 32
        status["lights"] = [{"id": quiet_govee, "provider": "govee"},
                            {"id": quiet_hue, "provider": "hue"}, {"id": "device-bad"}]
        lamps = build_preview(status, self.store.snapshot())["lamps"]
        for quiet in (quiet_govee, quiet_hue):
            self.assertEqual((lamps[quiet]["online"], lamps[quiet]["colors"]), (None, []))
        self.assertNotIn("device-bad", lamps)
        # The listed registry is the whole set: an empty one previews no lamps.
        self.assertEqual(build_preview({**status, "lights": []}, {})["lamps"], {})
        # Unknown ids never become lamps.
        status["hue"]["device-short"] = status["hue"][hue]
        self.assertNotIn("device-short", build_preview(status, {})["lamps"])

    def test_hue_dimming_delta_retains_color_and_color_mode_changes_clear_gradient(self):
        self.store.record_hue(
            "hue-monitor",
            {
                "gradient": {
                    "points": [
                        {"color": {"xy": {"x": 0.7, "y": 0.299}}},
                        {"color": {"xy": {"x": 0.17, "y": 0.7}}},
                    ]
                },
                "dimming": {"brightness": 50},
            },
        )
        first = self.lamp(key="hue-monitor")["colors"]
        self.assertEqual(len(first), 2)
        self.assertEqual(first[0], [128, 0, 0])
        self.store.record_hue("hue-monitor", {"dimming": {"brightness": 20}})
        self.assertEqual(self.lamp(key="hue-monitor")["colors"][0], [51, 0, 0])
        self.store.record_hue("hue-monitor", {"color_temperature": {"mirek": 250}})
        self.assertEqual(len(self.lamp(key="hue-monitor")["colors"]), 1)
        self.assertEqual(self.lamp(key="hue-monitor")["colors"], [[51, 47, 44]])
        self.store.record_hue("hue-monitor", {"color": {"xy": {"x": 0.7, "y": 0.299}}})
        self.assertEqual(self.lamp(key="hue-monitor")["colors"], [[51, 0, 0]])

    def test_govee_white_packet_replaces_segment_colors_with_its_emitted_temperature(self):
        self.send(self.device, "ptReal", segment_data([(255, 0, 0)] * 7))
        self.send(self.device, "brightness", {"value": 20})
        self.send(
            self.device, "colorwc", {"color": {"r": 0, "g": 0, "b": 0}, "colorTemInKelvin": 4000}
        )
        self.assertEqual(self.lamp()["colors"], [[51, 47, 44]])

    def test_a_govee_scene_shows_the_white_or_color_the_lamp_reports(self):
        # The core's last command (2150 K) is outside what the lamp shows now: it
        # reports 2700 K, set by another app or clamped to its range.
        self.send(self.device, "colorwc",
                  {"color": {"r": 0, "g": 0, "b": 0}, "colorTemInKelvin": 2150})
        self.now += 1  # The status below (0.1 s old) is newer than that command.

        def reported(color, kelvin):
            status = metadata()
            status["targets"][0]["native"].update(
                brightness=63, color=dict(zip("rgb", color)), colorTemInKelvin=kelvin)
            return status

        self.assertEqual(self.lamp(reported((0, 0, 0), 2700))["colors"], [[161, 139, 118]])
        self.assertEqual(self.lamp(reported((255, 0, 0), 0))["colors"], [[161, 0, 0]])
        # Without a reading (an effect mode reports neither) the command stays.
        self.assertEqual(self.lamp(reported((0, 0, 0), 0))["colors"], [[161, 133, 105]])
        # A status older than a new command does not hide it.
        self.send(self.device, "colorwc",
                  {"color": {"r": 0, "g": 0, "b": 255}, "colorTemInKelvin": 0})
        self.assertEqual(self.lamp(reported((0, 0, 0), 2700))["colors"], [[0, 0, 161]])
        # Segment colors of a scene are not replaced by the lamp's single color, even
        # by a newer status.
        self.send(self.device, "ptReal", segment_data([(255, 0, 0)] * 7))
        self.now += 1
        self.assertEqual(self.lamp(reported((0, 0, 0), 2700))["colors"], [[161, 0, 0]] * 7)
        # During Sync the lamp's scene reading never shows, even when newer.
        sync = self.store.wrap_govee(lambda *_: True, "sync-output")
        sync(self.device, "colorwc", {"color": {"r": 0, "g": 0, "b": 255}, "colorTemInKelvin": 0})
        self.now += 1
        status = reported((0, 0, 0), 2700)
        status["video"] = {"active": True, "ready": True, "frame_age": 0.1}
        self.assertEqual(self.lamp(status)["colors"], [[0, 0, 161]])

    def test_shared_source_handoff_never_resurrects_inactive_video_colors(self):
        self.send(
            self.device, "colorwc", {"color": {"r": 255, "g": 0, "b": 0}, "colorTemInKelvin": 0}
        )
        self.send(self.device, "brightness", {"value": 65})
        sync = self.store.wrap_govee(lambda *_: True, "sync-output")
        sync(self.device, "colorwc", {"color": {"r": 0, "g": 0, "b": 255}, "colorTemInKelvin": 0})
        status = metadata()
        status["video"] = {"active": True, "ready": True, "frame_age": 0.1}
        self.assertEqual(build_preview(status, self.store.snapshot())["mode"], "sync")
        self.assertEqual(self.lamp(status)["colors"], [[0, 0, 166]])
        self.assertEqual(self.lamp()["colors"], [])
        self.send(self.device, "brightness", {"value": 20})
        self.assertEqual(self.lamp()["colors"], [])
        self.send(
            self.device, "colorwc", {"color": {"r": 0, "g": 255, "b": 0}, "colorTemInKelvin": 0}
        )
        self.assertEqual(self.lamp()["colors"], [[0, 51, 0]])

    def test_off_stale_identity_foreign_and_waiting_mask_glow(self):
        self.send(self.device, "ptReal", segment_data([(255, 0, 0)] * 7))
        self.send(self.device, "brightness", {"value": 65})
        status = metadata()
        status["targets"][0]["native"]["onOff"] = 0
        self.assertIs(self.lamp(status)["on"], False)
        self.assertEqual(self.lamp(status)["colors"], [])
        for change in (
            {"status_age": 0.751},
            {"status_age": float("nan")},
            {"native": {"identity_verified": False, "onOff": 1}},
        ):
            status = metadata()
            status["targets"][0].update(change)
            self.assertIsNone(self.lamp(status)["on"])
            self.assertTrue(self.lamp(status)["stale"])
        status = metadata()
        status["hue"]["hue-monitor"]["native_mode"] = "streaming"
        self.assertEqual(build_preview(status, self.store.snapshot())["mode"], "foreign")
        self.assertEqual(self.lamp(status)["colors"], [])
        status["video"] = {"active": True, "ready": True, "frame_age": 0.6, "cleanup_pending": True}
        self.assertEqual(build_preview(status, self.store.snapshot())["mode"], "waiting")
        status["hue"]["hue-monitor"].update(native_age=2.01, native_mode="normal")
        self.assertIsNone(self.lamp(status, "hue-monitor")["online"])

    def test_native_channels_are_bytes_without_repeating_calibration_and_snapshot_is_copied(self):
        self.store.record_native_channels(
            {i: (32768 if i == 0 else 65535, 0, 0) for i in range(12)}
        )
        status = metadata()
        status["video"] = {"active": True, "ready": True, "frame_age": 0.2}
        self.assertEqual(self.lamp(status, "play-center")["colors"], [[128, 0, 0]])
        self.assertEqual(self.lamp(status, "play-left")["colors"], [[255, 0, 0]])
        self.assertEqual(len(self.lamp(status, "hue-monitor")["colors"]), 9)
        snapshot = self.store.snapshot()
        snapshot["play-center"]["colors"][0][0] = 0
        self.assertEqual(self.lamp(status, "play-center")["colors"], [[128, 0, 0]])

    def test_native_gain_does_not_override_scene_color_only_readback_brightness(self):
        self.store.record_native_channels({i: (32768, 0, 0) for i in range(12)})
        self.store.record_hue("play-center", {"color": {"xy": {"x": 0.7, "y": 0.299}}})
        status = metadata()
        status["hue"]["play-center"]["native_brightness"] = 25
        self.assertEqual(self.lamp(status, "play-center")["colors"], [[64, 0, 0]])
        self.assertEqual(self.lamp(status, "play-center")["brightness"], 25)
        self.store.record_hue("play-center", {"dimming": {"brightness": 12.5}})
        self.assertEqual(self.lamp(status, "play-center")["colors"], [[32, 0, 0]])

    def test_segment_checksum_masks_and_duplicates_are_rejected_atomically(self):
        self.send(self.device, "ptReal", segment_data([(255, 0, 0)] * 7))
        self.send(self.device, "brightness", {"value": 65})
        original = self.store.snapshot()
        for offset in (0, 7, 12, 19):
            encoded = segment_data([(0, 0, 255)] * 7)
            frame = bytearray(base64.b64decode(encoded["command"][0]))
            frame[offset] ^= 1
            encoded["command"][0] = base64.b64encode(frame).decode("ascii")
            self.send(self.device, "ptReal", encoded)
            self.assertEqual(self.store.snapshot(), original)
        encoded = segment_data([(0, 0, 255)] * 7)
        encoded["command"][1] = encoded["command"][0]
        self.send(self.device, "ptReal", encoded)
        self.assertEqual(self.store.snapshot(), original)

    def test_video_readiness_freshness_and_cleanup_are_required(self):
        self.store.record_native_channels({i: (65535, 0, 0) for i in range(12)})
        for video, mode in [
            ({"active": True, "ready": False, "frame_age": 0}, "waiting"),
            ({"active": True, "ready": True, "frame_age": 0.501}, "waiting"),
            ({"active": True, "ready": True, "frame_age": float("nan")}, "waiting"),
            ({"active": False, "ready": True, "frame_age": 0}, "waiting"),
            ({"active": True, "ready": True, "frame_age": 0, "cleanup_pending": True}, "waiting"),
        ]:
            status = metadata()
            status["video"] = video
            result = build_preview(status, self.store.snapshot())
            self.assertEqual(result["mode"], mode)
            self.assertEqual(result["lamps"]["play-center"]["colors"], [])
        status["video"] = {"active": True, "ready": True, "frame_age": 0.5}
        status["hue"]["play-center"]["native_age"] = 2
        self.assertEqual(self.lamp(status, "play-center")["colors"], [[255, 0, 0]])

    def test_expired_lease_waits_while_our_native_relay_is_still_held(self):
        self.store.record_hue(
            "hue-monitor",
            {
                "color": {"xy": {"x": 0.7, "y": 0.299}},
                "dimming": {"brightness": 50},
            },
        )
        status = metadata()
        status["hue"]["hue-monitor"]["native_mode"] = "streaming"
        status["video"] = {
            "active": False,
            "ready": True,
            "native_owned": True,
            "cleanup_pending": False,
            "last_result": "streaming",
            "frame_age": 0.6,
        }
        result = build_preview(status, self.store.snapshot())
        self.assertEqual(result["mode"], "waiting")
        self.assertEqual(result["lamps"]["hue-monitor"]["colors"], [])
        status["video"]["ready"] = False
        self.assertEqual(build_preview(status, self.store.snapshot())["mode"], "waiting")
        status["video"]["native_owned"] = False
        self.assertEqual(build_preview(status, self.store.snapshot())["mode"], "foreign")
        status["hue"]["hue-monitor"]["native_mode"] = "normal"
        self.assertEqual(build_preview(status, self.store.snapshot())["mode"], "scene")

    def test_short_govee_status_miss_keeps_the_glow_until_the_loss_lasts(self):
        self.send(self.device, "ptReal", segment_data([(255, 0, 0)] * 7))
        self.send(self.device, "brightness", {"value": 65})

        def missed(verified_age, on=1, verified=True):
            status = metadata()
            status["targets"][0].update(
                status_age=0.05,
                native={"identity_verified": False, "onOff": 0},
                verified_native={"identity_verified": verified, "onOff": on, "brightness": 65},
                verified_age=verified_age)
            return self.lamp(status)

        held = missed(1.6)
        self.assertEqual((held["online"], held["on"], held["stale"]), (True, True, False))
        self.assertEqual(held["colors"], self.lamp()["colors"])
        self.assertTrue(held["colors"])
        off = missed(1.6, on=0)
        self.assertEqual((off["on"], off["colors"]), (False, []))
        for lost in (missed(2.01), missed(None), missed(float("nan")), missed(1.6, verified=False)):
            self.assertEqual((lost["online"], lost["on"], lost["stale"]), (None, None, True))
            self.assertEqual(lost["colors"], [])

    def test_malformed_ages_and_untrusted_ready_flags_fail_closed(self):
        for age in (10**1000, -1, True, "secret", None):
            status = metadata()
            status["targets"][0]["status_age"] = age
            status["hue"]["hue-monitor"]["native_age"] = age
            result = build_preview(status, {})
            self.assertIsNone(result["lamps"]["govee-left"]["online"])
            self.assertIsNone(result["lamps"]["hue-monitor"]["online"])
        status = metadata()
        status["hue"]["hue-monitor"]["native_mode"] = "streaming"
        status["video"] = {"native_owned": "untrusted"}
        self.assertEqual(build_preview(status, {})["mode"], "foreign")

    def test_projection_is_bounded_secret_free_and_rejects_malformed_output(self):
        status = metadata()
        status["token"] = "secret-token"
        status["targets"][0].update(device="secret-device", ip="secret-ip")
        output = {
            "govee-left": {
                "colors": [[999, 0, 0]],
                "brightness": 20,
                "source": "secret-token",
                "color_source": "scene-output",
            },
            "secret-device": {"secret": "secret-ip"},
        }
        public = build_preview(status, output)
        self.assertEqual(
            set(public["lamps"]),
            {"govee-left", "govee-right", "hue-monitor", "play-left", "play-center", "play-right"},
        )
        self.assertNotIn("secret", json.dumps(public))
        self.assertEqual(public["lamps"]["govee-left"]["colors"], [])


class ProducerTests(unittest.TestCase):
    def test_proxy_records_only_calibrated_accepted_writes_and_observer_cannot_break_success(self):
        store = OutputPreviewStore()
        client = FakeHue()
        proxy = HueStripProxy(
            client, "hue-monitor", clock=lambda: 10, wall_clock=lambda: 1000, preview=store
        )
        proxy.submit(command())
        self.assertTrue(proxy.process_once())
        self.assertEqual(store.snapshot()["hue-monitor"]["brightness"], 198 / 254 * 100)
        old = store.snapshot()
        client.write = lambda _: (_ for _ in ()).throw(OSError("failure"))
        proxy.submit(command(sequence=2))
        self.assertFalse(proxy.process_once())
        self.assertEqual(store.snapshot(), old)
        client.write = lambda _: None
        proxy.preview = BrokenObserver()
        self.assertTrue(proxy.process_once())
        self.assertEqual(proxy.applied, 2)
        self.assertEqual(proxy.snapshot()["writes"], 2)

    def test_proxy_records_layout_calibrated_center_put_without_double_scaling(self):
        class Layout:
            def scale(self, role, fallback):
                return 0.25

            def snapshot(self):
                return None

        store = OutputPreviewStore()
        client = FakeHue()
        client.resource_id = PLAY_IDS["play-center"]
        client.native["id"] = client.resource_id
        proxy = HueStripProxy(
            client,
            "play-center",
            clock=lambda: 10,
            wall_clock=lambda: 1000,
            layout_store=Layout(),
            preview=store,
        )
        proxy.submit(
            command(
                light="play-center",
                state={
                    "on": True,
                    "bri": 254,
                    "colormode": "xy",
                    "xy": [0.7, 0.299],
                    "_changed": ["bri", "xy"],
                },
            )
        )
        self.assertTrue(proxy.process_once())
        self.assertEqual(client.writes[-1]["dimming"]["brightness"], 25)
        self.assertEqual(
            build_preview(metadata(), store.snapshot())["lamps"]["play-center"]["colors"],
            [[64, 0, 0]],
        )

    def test_proxy_shows_the_color_a_hue_light_has_without_a_write_of_its_own(self):
        # After an adapter restart, or a scene set in another app, nothing passes through
        # record_hue(); the proxy's read-back keeps the preview true to the lamp.
        store = OutputPreviewStore()
        client = FakeHue()
        client.resource_id = PLAY_IDS["play-center"]
        client.native.update(id=client.resource_id, color={"xy": {"x": 0.7, "y": 0.299}},
                             color_temperature={"mirek": None, "mirek_valid": False},
                             dimming={"brightness": 50})
        proxy = HueStripProxy(client, "play-center", clock=lambda: 10, wall_clock=lambda: 1000,
                              preview=store)
        self.assertFalse(proxy.process_once())
        self.assertEqual(client.writes, [], "a read-back writes nothing")
        lamp = build_preview(metadata(), store.snapshot())["lamps"]["play-center"]
        self.assertEqual(lamp["colors"], [[128, 0, 0]])
        self.assertFalse(lamp["stale"])
        client.native["color_temperature"] = {"mirek": 250, "mirek_valid": True}
        proxy.process_once()
        lamp = build_preview(metadata(), store.snapshot())["lamps"]["play-center"]
        self.assertEqual(lamp["colors"], [[128, 118, 110]], "a white light shows its temperature")
        client.native["mode"] = "streaming"
        proxy.process_once()
        self.assertEqual(store.snapshot()["play-center"]["colors"], [[255, 235, 219]],
                         "a streaming light keeps the last read-back")

    def test_a_light_outside_sync_shows_its_scene_in_every_mode(self):
        # A Hue light for scenes only (another room) is outside the Entertainment
        # area: the read-back is recorded for it, and its scene shows whatever Sync does.
        strip = "device-" + "a" * 32
        store = OutputPreviewStore(native_channels={"play-left": (0,)}, scene_lights=[strip])
        native = {"color": {"xy": {"x": 0.7, "y": 0.299}}, "dimming": {"brightness": 50},
                  "color_temperature": {"mirek": None, "mirek_valid": False}}
        store.record_hue_read_back(strip, native)
        store.record_hue_read_back("device-" + "b" * 32, native)
        self.assertEqual(set(store.snapshot()), {strip}, "only a known Hue light is recorded")
        status = metadata()
        status["lights"] = [{"id": "play-left", "provider": "hue"},
                            {"id": strip, "provider": "hue"}]
        status["hue"][strip] = copy.deepcopy(status["hue"]["play-left"])
        status["composition"] = {"enabled_roles": ["play-left"], "lights": ["play-left"]}
        for video in ({}, {"active": True, "ready": True, "frame_age": 0},
                      {"active": True, "ready": False}):
            with self.subTest(video=video):
                lamps = build_preview({**status, "video": video}, store.snapshot())["lamps"]
                self.assertEqual(lamps[strip]["colors"], [[128, 0, 0]])
                self.assertEqual((lamps[strip]["stale"], lamps[strip]["included"]),
                                 (False, False))
        # Another app streaming to it hides the last read-back.
        status["hue"][strip]["native_mode"] = "streaming"
        lamps = build_preview(status, store.snapshot())["lamps"]
        self.assertEqual((lamps[strip]["colors"], lamps[strip]["stale"]), ([], True))
        # Sync output left from when the light was in Sync is not shown.
        status["hue"][strip]["native_mode"] = "normal"
        store.native_channels[strip] = (1,)
        store.record_native_channels({0: (65535, 0, 0), 1: (0, 65535, 0)})
        lamps = build_preview(status, store.snapshot())["lamps"]
        self.assertEqual((lamps[strip]["colors"], lamps[strip]["stale"]), ([], True))

    def test_a_gradient_read_back_shows_its_points(self):
        store = OutputPreviewStore()
        client = FakeHue()
        client.native["gradient"]["points"] = [{"color": {"xy": {"x": 0.7, "y": 0.299}}},
                                               {"color": {"xy": {"x": 0.17, "y": 0.7}}}]
        client.native["color"] = {"xy": {"x": 0.4, "y": 0.4}}
        proxy = HueStripProxy(client, "hue-monitor", clock=lambda: 10, wall_clock=lambda: 1000,
                              preview=store)
        proxy.process_once()
        lamp = build_preview(metadata(), store.snapshot())["lamps"]["hue-monitor"]
        self.assertEqual(len(lamp["colors"]), 2)

    def test_relay_records_actual_calibrated_mapping_after_send_and_observer_is_isolated(self):
        fixture = relay_tests.RelayTests()
        fixture.setUp()
        store = OutputPreviewStore()
        fixture.relay = HueEntertainmentRelay(
            fixture.config,
            requester=fixture.request,
            connection_factory=lambda _: fixture.connection,
            preview=store,
            profile=NativeHueProfile.from_dict(profile_data()),
        )
        fixture.relay.start()
        frame = {i: (65535, i * 1000, 0) for i in range(14)}
        fixture.relay.send(frame)
        status = metadata()
        status["video"] = {"active": True, "ready": True, "frame_age": 0}
        lamps = build_preview(status, store.snapshot())["lamps"]
        self.assertEqual(lamps["play-center"]["colors"], [[128, 23, 0]])
        self.assertEqual(lamps["play-left"]["colors"], [[255, 43, 0]])
        self.assertEqual(lamps["play-right"]["colors"], [[255, 51, 0]])
        self.assertEqual(lamps["hue-monitor"]["colors"][0], [255, 8, 0])
        old = store.snapshot()
        fixture.connection.send = lambda _: (_ for _ in ()).throw(OSError("failure"))
        with self.assertRaises(OSError):
            fixture.relay.send(frame)
        self.assertEqual(store.snapshot(), old)
        self.assertEqual(fixture.relay.sequence, 1)
        fixture.connection.send = lambda _: None
        fixture.relay.preview = BrokenObserver()
        fixture.relay.send(frame)
        self.assertEqual(fixture.relay.sequence, 2)

    def test_govee_observer_failure_does_not_change_sender_return_or_render(self):
        class BrokenStore(OutputPreviewStore):
            def _record_govee(self, *args):
                raise RuntimeError("observer failed")

        store = BrokenStore()
        controller = GoveeController(
            targets(),
            sender=store.wrap_govee(lambda *_: True, "scene-output"),
            clock=lambda: 10,
            wall_clock=lambda: 1000,
        )
        controller.update_statuses({LEFT: ({"onOff": 1, "brightness": 65}, 10)})
        controller.submit(
            {
                "light": "govee-left",
                "session": "test",
                "sequence": 1,
                "issued_at": 1000,
                "transitiontime": 0,
                "state": {"on": True, "bri": 254, "colormode": "rgb", "rgb": [255, 0, 0]},
            }
        )
        controller.render()
        self.assertEqual(controller.snapshot()["commands_sent"], 2)
        self.assertEqual(controller.snapshot()["rejected_sends"]["govee-left"], 0)

    def test_video_snapshot_distinguishes_live_fence_from_cleanup_and_handshake_waiting(self):
        fixture = video_tests.VideoTests()
        fixture.setUp()
        for target in fixture.controller.targets:
            target["segment_count"] = 7 if GRADIENT_MODE else 0
        fixture.lease.submit(
            envelope(colors=[[i, 65535, 32768, 0] for i in range(18 if GRADIENT_MODE else 14)])
        )
        self.assertTrue(fixture.output.process_once())
        self.assertTrue(fixture.output.cleanup_pending)
        status = metadata()
        status["video"] = fixture.output.snapshot()
        self.assertEqual(build_preview(status, {})["mode"], "sync")
        self.assertFalse(status["video"]["cleanup_pending"])
        fixture.output.last_result = "waiting for fresh native state after handshake"
        status["video"] = fixture.output.snapshot()
        self.assertEqual(build_preview(status, {})["mode"], "waiting")
        fixture.output.ready = False
        fixture.relay.stop = lambda: (_ for _ in ()).throw(OSError("cleanup unavailable"))
        status["video"] = fixture.output.snapshot()
        self.assertTrue(status["video"]["cleanup_pending"])
        self.assertEqual(build_preview(status, {})["mode"], "waiting")

    def test_main_wires_one_passive_store_across_all_output_paths_without_starting_io(self):
        captured = {}

        def capture_handler(*args, **kwargs):
            captured.update(controller=args[0], proxies=args[2], **kwargs)
            return object()

        def client(_config, resource_id=None, profile=None, link=None):
            self.assertIsNotNone(link)  # the key goes only over the verified link
            # Every Hue light, the scenes-only ones too (#78).
            self.assertEqual(profile.light_ids,
                             NativeHueProfile.from_dict(profile_data()).light_ids)
            result = FakeHue()
            result.profile = profile
            if resource_id is not None:
                result.resource_id = resource_id
                result.native["id"] = resource_id
            return result

        class StopBeforeIO(Exception):
            pass

        with TemporaryDirectory(prefix="tmp_preview_wiring_") as directory:
            root = Path(directory)
            write_registry(root, targets(), profile_data())
            (root / "token").write_text("a" * 48)
            (root / "hue.json").write_text('{"bridge":"192.168.99.2"}')
            fixture = relay_tests.RelayTests()
            fixture.setUp()
            fixture.config["bridge"] = "192.168.99.2"
            (root / "stream.json").write_text(json.dumps(fixture.config))
            args = [
                "--registry",
                str(root / "lights.json"),
                "--token-file",
                str(root / "token"),
                "--hue-config",
                str(root / "hue.json"),
                "--hue-play",
                "--hue-stream-config",
                str(root / "stream.json"),
            ]
            with (
                patch.object(adapter, "PinnedHueClient", client),
                patch.object(adapter, "handler_for", capture_handler),
                patch.object(adapter, "ThreadingHTTPServer", side_effect=StopBeforeIO),
                patch.object(adapter.lan, "send_to_target", lambda *_: True),
            ):
                with self.assertRaises(StopBeforeIO):
                    adapter.main(args)
        store = captured["preview"]
        proxies = captured["proxies"]
        self.assertEqual(set(proxies), {"hue-monitor", "play-left", "play-center", "play-right"})
        self.assertTrue(all(proxies[light].light == light for light in proxies))
        self.assertTrue(all(proxy.preview is store for proxy in proxies.values()))
        self.assertIs(captured["video_output"].relay.preview, store)
        device = targets()[0]
        captured["controller"].sender(device, "ptReal", segment_data([(255, 0, 0)] * 7))
        captured["video_output"].controller.sender(device, "brightness", {"value": 30})
        sample = store.snapshot()["govee-left"]
        self.assertEqual(sample["colors"], [[255, 0, 0]] * 7)
        self.assertEqual(sample["brightness"], 30)
        self.assertEqual(sample["source"], "sync-output")

    def test_broken_status_observer_cannot_break_status_response(self):
        observer = type(
            "Observer",
            (),
            {"snapshot": lambda _: (_ for _ in ()).throw(RuntimeError("observer failed"))},
        )()
        controller = type("Controller", (), {"snapshot": lambda _: metadata()})()
        handler = handler_for(controller, "test-token", preview=observer)
        request = object.__new__(handler)
        request.path = "/status"
        request.headers = {"X-Adapter-Token": "test-token"}
        responses = []
        request.respond = lambda code, body: responses.append((code, body))
        request.do_GET()
        self.assertEqual(responses[0][0], 200)
        self.assertEqual(responses[0][1]["preview"]["lamps"]["govee-left"]["colors"], [])

    def test_authenticated_status_preview_uses_assembled_snapshot_without_io(self):
        store = OutputPreviewStore()
        controller = type("Controller", (), {"snapshot": lambda _: metadata()})()
        handler = handler_for(controller, "test-token", preview=store)
        request = object.__new__(handler)
        request.path = "/status"
        request.headers = {"X-Adapter-Token": "test-token"}
        responses = []
        request.respond = lambda code, body: responses.append((code, body))
        request.do_GET()
        self.assertEqual(responses[0][0], 200)
        self.assertEqual(responses[0][1]["preview"]["version"], 1)
        request.headers = {}
        request.do_GET()
        self.assertEqual(responses[-1], (403, {"error": "unauthorized"}))


class BrokenObserver:
    def record_hue(self, *args):
        raise RuntimeError("observer failed")

    def record_native_channels(self, *args):
        raise RuntimeError("observer failed")
