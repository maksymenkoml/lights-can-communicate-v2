import io
import unittest
from contextlib import redirect_stdout
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

    def test_palette_update_preserves_homebridge_power_and_brightness(self):
        commands = lighting.govee_commands({"on": True, "brightness": 30, "rgb": (20, 60, 255)})
        self.assertEqual([name for name, _ in commands], ["colorwc"])

    def test_invalid_chromaticity_is_rejected(self):
        for x, y in ((0.1, 0), (float("nan"), 0.3), (-0.1, 0.3)):
            with self.subTest(x=x, y=y), self.assertRaises(ValueError):
                lighting.xy_to_rgb(x, y)


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
                )
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
                )
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
                )
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
