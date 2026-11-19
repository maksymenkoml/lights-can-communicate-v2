import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge"))
from entertainment_stream import native_colors  # noqa: E402
from hue_strip_proxy import HueStripProxy  # noqa: E402
from live_adapter import GoveeController  # noqa: E402
from test_hue_strip_proxy import FakeHue  # noqa: E402
from test_live_adapter import LEFT, RIGHT, command, targets  # noqa: E402
from test_native_profile import PLAY_IDS  # noqa: E402


class Scales:
    def __init__(self):
        self.values = {"left": 0.65, "right": 0.65, "hue_center": 0.5}

    def scale(self, role, fallback):
        return self.values.get(role, fallback)


class CalibrationTests(unittest.TestCase):
    def test_first_transition_does_not_scale_physical_native_brightness_twice(self):
        scales = Scales()
        scales.values["left"] = 0.4
        now = [0]
        controller = GoveeController(
            targets(),
            sender=lambda *args: True,
            clock=lambda: now[0],
            wall_clock=lambda: 1000,
            layout_store=scales,
        )
        controller.update_statuses(
            {LEFT: ({"onOff": 1, "brightness": 40, "color": {"r": 255, "g": 0, "b": 0}}, 0)}
        )
        controller.submit(command(transitiontime=20))
        self.assertEqual(controller.effective_target("left")["brightness"], 40)
        now[0] = 2
        self.assertEqual(controller.effective_target("left")["brightness"], 40)

    def test_govee_scale_changes_without_new_color_and_off_stays_blocked(self):
        scales = Scales()
        sent = []
        controller = GoveeController(
            targets(),
            sender=lambda dev, name, data: sent.append((name, data)) or True,
            clock=lambda: 0,
            wall_clock=lambda: 1000,
            layout_store=scales,
        )
        controller.update_statuses(
            {LEFT: ({"onOff": 1, "brightness": 65}, 0), RIGHT: ({"onOff": 0}, 0)}
        )
        controller.submit(command(transitiontime=0))
        controller.render()
        sent.clear()
        scales.values["left"] = 0.4
        controller.render()
        self.assertEqual(sent, [("brightness", {"value": 40})])
        sent.clear()
        controller.update_statuses({LEFT: ({"onOff": 0}, 0)})
        scales.values["left"] = 0.8
        controller.render()
        self.assertEqual(sent, [])

    def test_native_scene_scale_updates_existing_desired_state_without_power(self):
        scales = Scales()
        client = FakeHue()
        client.resource_id = PLAY_IDS["hue_center"]
        client.native["id"] = client.resource_id
        proxy = HueStripProxy(
            client, role="hue_center", clock=lambda: 0, wall_clock=lambda: 1000, layout_store=scales
        )
        proxy.submit(command(side="hue_center", transitiontime=0))
        proxy.process_once()
        self.assertEqual(client.writes[-1]["dimming"]["brightness"], 50)
        scales.values["hue_center"] = 0.25
        self.assertTrue(proxy.process_once())
        self.assertEqual(client.writes[-1]["dimming"]["brightness"], 25)
        count = len(client.writes)
        client.native["on"]["on"] = False
        scales.values["hue_center"] = 0.8
        proxy.process_once()
        self.assertEqual(len(client.writes), count)

    def test_video_native_scales_are_per_role_and_keep_existing_channel_mapping(self):
        frame = {i: (10000, 20000, 30000) for i in range(18)}
        out = native_colors(
            frame, scales={"hue_center": 0.25, "hue_left": 0.8, "hue_right": 1, "strip": 0.6}
        )
        self.assertEqual(out[0], (2500, 5000, 7500))
        self.assertEqual(out[1], (8000, 16000, 24000))
        self.assertEqual(out[2], frame[13])
        self.assertEqual(out[3], (6000, 12000, 18000))
