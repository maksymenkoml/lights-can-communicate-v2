import io
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import smart_lighting as lighting


def gradient_light():
    return {
        "on": {"on": True},
        "dimming": {"brightness": 42.4},
        "color": {"xy": {"x": 0.3127, "y": 0.3290}},
        "gradient": {
            "points": [
                {"color": {"xy": {"x": 0.7, "y": 0.299}}},
                {"color": {"xy": {"x": 0.15, "y": 0.06}}},
            ]
        },
    }


class PaletteTests(unittest.TestCase):
    def test_gradient_endpoints_produce_red_left_and_blue_right(self):
        left = lighting.target_for(gradient_light(), "left")
        right = lighting.target_for(gradient_light(), "right")
        self.assertEqual(left["brightness"], 42)
        self.assertGreater(left["rgb"][0], 240)
        self.assertLess(left["rgb"][2], 20)
        self.assertGreater(right["rgb"][2], 240)
        self.assertLess(right["rgb"][0], 20)

    def test_single_color_has_same_palette_on_both_sides(self):
        light = gradient_light()
        light.pop("gradient")
        left = lighting.target_for(light, "left")
        self.assertEqual(left, lighting.target_for(light, "right"))
        self.assertLess(max(left["rgb"]) - min(left["rgb"]), 3)

    def test_hue_off_needs_no_color_and_never_switches_govee(self):
        target = lighting.target_for({"on": {"on": False}}, "left")
        self.assertEqual(lighting.govee_commands(target), [])

    def test_active_white_temperature_overrides_stale_gradient(self):
        light = gradient_light()
        light["color_temperature"] = {"mirek_valid": True, "mirek": 250}
        target = lighting.target_for(light, "left")
        self.assertEqual(target["kelvin"], 4000)
        self.assertNotIn("rgb", target)

    def test_invalid_white_temperature_uses_gradient(self):
        light = gradient_light()
        light["color_temperature"] = {"mirek_valid": False, "mirek": 250}
        self.assertIn("rgb", lighting.target_for(light, "left"))

    def test_rgb_command_disables_white_temperature(self):
        commands = lighting.govee_commands({"on": True, "brightness": 30, "rgb": (20, 60, 255)})
        self.assertIn(
            ("colorwc", {"color": {"r": 20, "g": 60, "b": 255}, "colorTemInKelvin": 0}),
            commands,
        )

    def test_palette_update_copies_brightness_without_switching_power(self):
        commands = lighting.govee_commands({"on": True, "brightness": 30, "rgb": (20, 60, 255)})
        self.assertEqual([name for name, _ in commands], ["colorwc", "brightness"])
        self.assertIn(("brightness", {"value": 30}), commands)

    def test_low_hue_brightness_uses_minimum_govee_brightness_without_turning_off(self):
        light = gradient_light()
        light["dimming"]["brightness"] = 0.1
        commands = lighting.govee_commands(lighting.target_for(light, "left"))
        self.assertIn(("brightness", {"value": 1}), commands)
        self.assertNotIn("turn", [name for name, _ in commands])

    def test_invalid_chromaticity_is_rejected(self):
        for x, y in ((0.1, 0), (float("nan"), 0.3), (-0.1, 0.3)):
            with self.subTest(x=x, y=y), self.assertRaises(ValueError):
                lighting.xy_to_rgb(x, y)


class CalibrationTests(unittest.TestCase):
    def test_calibration_command_persists_each_side_and_preserves_pairing(self):
        with (
            TemporaryDirectory(prefix="tmp_lighting_test_") as directory,
            patch.object(lighting, "CONFIG", Path(directory) / ".lighting.json"),
            patch(
                "sys.argv", ["smart_lighting.py", "calibrate", "--left", "0.5", "--right", "0.4"]
            ),
            redirect_stdout(io.StringIO()),
        ):
            lighting.save_config(
                {
                    "hue_key": "test-key",
                    "targets": [
                        {"ip": "192.168.10.50", "side": "left"},
                        {"ip": "192.168.10.51", "side": "right"},
                    ],
                }
            )
            lighting.main()
            config = lighting.load_config()
            self.assertEqual(
                [device["brightness_scale"] for device in config["targets"]], [0.5, 0.4]
            )
            self.assertEqual(config["hue_key"], "test-key")

    def test_calibration_scales_hue_brightness_proportionally(self):
        light = gradient_light()
        for source, expected in ((100, 50), (80, 40), (20, 10), (0.1, 1)):
            with self.subTest(source=source):
                light["dimming"]["brightness"] = source
                target = lighting.target_for(light, "left", brightness_scale=0.5)
                self.assertEqual(target["brightness"], expected)
                self.assertEqual(target["rgb"], (255, 0, 0))
                self.assertTrue(target["on"])

    def test_invalid_scale_is_rejected(self):
        for scale in (0, -0.5, 1.1, float("nan"), float("inf")):
            with self.subTest(scale=scale), self.assertRaises(ValueError):
                lighting.target_for(gradient_light(), "left", brightness_scale=scale)

    def test_each_govee_can_have_a_different_scale(self):
        config = {
            "light": "test-strip",
            "targets": [
                {"ip": "192.168.10.50", "side": "left", "brightness_scale": 0.5},
                {"ip": "192.168.10.51", "side": "right", "brightness_scale": 0.25},
            ],
        }
        sent = []
        with (
            patch.object(lighting, "hue_request", return_value={"data": [gradient_light()]}),
            patch.object(lighting, "govee_status", return_value={"onOff": 1}),
            patch.object(lighting, "govee_send", side_effect=lambda *args: sent.append(args)),
            redirect_stdout(io.StringIO()),
        ):
            lighting.run_sync(config, once=True)
        self.assertEqual(
            [packet for packet in sent if packet[1] == "brightness"],
            [
                ("192.168.10.50", "brightness", {"value": 21}),
                ("192.168.10.51", "brightness", {"value": 11}),
            ],
        )
        self.assertNotIn("turn", [packet[1] for packet in sent])

    def test_dynamic_frames_keep_the_calibrated_brightness(self):
        light = gradient_light()
        light["dynamics"] = {"status": "dynamic_palette", "speed": 1, "speed_valid": True}
        targets = [{"ip": "192.168.10.50", "side": "left", "brightness_scale": 0.5}]
        previous = {}
        with (
            patch.object(lighting, "govee_status", return_value={"onOff": 1}),
            patch.object(lighting, "govee_send"),
        ):
            lighting.animate_frame(light, targets, elapsed=2, previous=previous)
        self.assertEqual(previous["192.168.10.50"]["brightness"], 21)


class DynamicPaletteTests(unittest.TestCase):
    def test_dynamic_palette_visits_both_colors_and_wraps(self):
        light = gradient_light()
        light["dynamics"] = {"status": "dynamic_palette", "speed": 1, "speed_valid": True}
        self.assertEqual(lighting.target_for(light, "left", elapsed=0)["rgb"], (255, 0, 0))
        self.assertEqual(lighting.target_for(light, "left", elapsed=2)["rgb"], (0, 0, 255))
        self.assertEqual(lighting.target_for(light, "left", elapsed=4)["rgb"], (255, 0, 0))

    def test_transition_has_intermediate_colors_and_opposite_side_offset(self):
        light = gradient_light()
        light["dynamics"] = {"status": "dynamic_palette", "speed": 1, "speed_valid": True}
        middle = lighting.target_for(light, "left", elapsed=1)["rgb"]
        self.assertEqual(middle[0], 255)
        self.assertEqual(middle[1], 0)
        self.assertGreater(middle[2], 100)
        self.assertLess(middle[2], 250)
        self.assertEqual(lighting.target_for(light, "right", elapsed=0)["rgb"], (0, 0, 255))
        self.assertEqual(lighting.target_for(light, "right", elapsed=2)["rgb"], (255, 0, 0))

    def test_stopping_play_restores_static_palette(self):
        light = gradient_light()
        light["dynamics"] = {"status": "none", "speed": 1, "speed_valid": True}
        self.assertEqual(lighting.target_for(light, "left", elapsed=2)["rgb"], (255, 0, 0))

    def test_hue_speed_controls_the_approximate_cycle(self):
        light = gradient_light()
        light["dynamics"] = {"status": "dynamic_palette", "speed": 0, "speed_valid": True}
        self.assertEqual(lighting.target_for(light, "left", elapsed=12)["rgb"], (0, 0, 255))

    def test_animation_frame_skips_off_and_unreachable_govee(self):
        light = gradient_light()
        light["dynamics"] = {"status": "dynamic_palette", "speed": 1, "speed_valid": True}
        targets = [{"ip": "192.168.10.50", "side": "left"}]
        sent = []
        with (
            patch.object(lighting, "govee_status", side_effect=[{"onOff": 0}, None]),
            patch.object(lighting, "govee_send", side_effect=lambda *args: sent.append(args)),
        ):
            lighting.animate_frame(light, targets, elapsed=1, previous={})
            lighting.animate_frame(light, targets, elapsed=2, previous={})
        self.assertEqual(sent, [])

    def test_animation_frame_only_changes_color(self):
        light = gradient_light()
        light["dynamics"] = {"status": "dynamic_palette", "speed": 1, "speed_valid": True}
        targets = [{"ip": "192.168.10.50", "side": "left"}]
        sent = []
        with (
            patch.object(lighting, "govee_status", return_value={"onOff": 1}),
            patch.object(lighting, "govee_send", side_effect=lambda *args: sent.append(args)),
        ):
            lighting.animate_frame(light, targets, elapsed=2, previous={})
        self.assertEqual(
            sent,
            [
                (
                    "192.168.10.50",
                    "colorwc",
                    {"color": {"r": 0, "g": 0, "b": 255}, "colorTemInKelvin": 0},
                )
            ],
        )


class HomebridgeTests(unittest.TestCase):
    def test_following_recovers_after_one_missing_udp_reply(self):
        config = {"light": "test-strip", "targets": [{"ip": "192.168.10.50", "side": "left"}]}
        sent = []
        with (
            patch.object(
                lighting,
                "hue_request",
                side_effect=[
                    {"data": [gradient_light()]},
                    {"data": [gradient_light()]},
                    KeyboardInterrupt,
                ],
            ),
            patch.object(lighting, "govee_status", side_effect=[None, {"onOff": 1}]),
            patch.object(lighting, "govee_send", side_effect=lambda *args: sent.append(args)),
            patch.object(lighting.time, "sleep"),
            redirect_stdout(io.StringIO()),
            self.assertRaises(KeyboardInterrupt),
        ):
            lighting.run_sync(config)
        self.assertEqual(
            sent,
            [
                (
                    "192.168.10.50",
                    "colorwc",
                    {"color": {"r": 255, "g": 0, "b": 0}, "colorTemInKelvin": 0},
                ),
                ("192.168.10.50", "brightness", {"value": 42}),
            ],
        )

    def test_offline_left_does_not_block_online_right(self):
        config = {
            "light": "test-strip",
            "targets": [
                {"ip": "192.168.10.50", "side": "left"},
                {"ip": "192.168.10.51", "side": "right"},
            ],
        }
        sent = []
        with (
            patch.object(
                lighting,
                "hue_request",
                side_effect=[{"data": [gradient_light()]}, KeyboardInterrupt],
            ),
            patch.object(lighting, "govee_status", side_effect=[None, {"onOff": 1}]),
            patch.object(lighting, "govee_send", side_effect=lambda *args: sent.append(args)),
            patch.object(lighting.time, "sleep"),
            redirect_stdout(io.StringIO()),
            self.assertRaises(KeyboardInterrupt),
        ):
            lighting.run_sync(config)
        self.assertEqual(
            sent,
            [
                (
                    "192.168.10.51",
                    "colorwc",
                    {"color": {"r": 0, "g": 0, "b": 255}, "colorTemInKelvin": 0},
                ),
                ("192.168.10.51", "brightness", {"value": 42}),
            ],
        )

    def test_streaming_hue_is_skipped_without_touching_govee(self):
        light = gradient_light()
        light["mode"] = "streaming"
        config = {"light": "test-strip", "targets": [{"ip": "192.168.10.50", "side": "left"}]}
        sent = []
        with (
            patch.object(lighting, "hue_request", return_value={"data": [light]}),
            patch.object(lighting, "govee_send", side_effect=lambda *args: sent.append(args)),
            redirect_stdout(io.StringIO()),
        ):
            lighting.run_sync(config, once=True)
        self.assertEqual(sent, [])

    def test_following_resumes_when_hue_sync_ends(self):
        streaming = gradient_light()
        streaming["mode"] = "streaming"
        config = {"light": "test-strip", "targets": [{"ip": "192.168.10.50", "side": "left"}]}
        sent = []
        with (
            patch.object(
                lighting,
                "hue_request",
                side_effect=[
                    {"data": [streaming]},
                    {"data": [gradient_light()]},
                    KeyboardInterrupt,
                ],
            ),
            patch.object(lighting, "govee_status", return_value={"onOff": 1}),
            patch.object(lighting, "govee_send", side_effect=lambda *args: sent.append(args)),
            patch.object(lighting.time, "sleep"),
            redirect_stdout(io.StringIO()),
            self.assertRaises(KeyboardInterrupt),
        ):
            lighting.run_sync(config)
        self.assertEqual(
            sent,
            [
                (
                    "192.168.10.50",
                    "colorwc",
                    {"color": {"r": 255, "g": 0, "b": 0}, "colorTemInKelvin": 0},
                ),
                ("192.168.10.50", "brightness", {"value": 42}),
            ],
        )

    def test_off_govee_receives_no_commands_even_when_hue_is_on(self):
        config = {"light": "test-strip", "targets": [{"ip": "192.168.10.50", "side": "left"}]}
        sent = []
        with (
            patch.object(lighting, "hue_request", return_value={"data": [gradient_light()]}),
            patch.object(lighting, "govee_status", return_value={"onOff": 0}),
            patch.object(lighting, "govee_send", side_effect=lambda *args: sent.append(args)),
            redirect_stdout(io.StringIO()),
        ):
            lighting.run_sync(config, once=True)
        self.assertEqual(sent, [])

    def test_unknown_govee_state_never_receives_color_commands(self):
        config = {"light": "test-strip", "targets": [{"ip": "192.168.10.50", "side": "left"}]}
        sent = []
        with (
            patch.object(lighting, "hue_request", return_value={"data": [gradient_light()]}),
            patch.object(lighting, "govee_status", return_value=None),
            patch.object(lighting, "govee_send", side_effect=lambda *args: sent.append(args)),
            redirect_stdout(io.StringIO()),
            self.assertRaises(ValueError),
        ):
            lighting.run_sync(config, once=True)
        self.assertEqual(sent, [])


if __name__ == "__main__":
    unittest.main()
