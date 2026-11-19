"""Exercise real video-to-Govee routing in each selected stream profile."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge"))
from entertainment_stream import GRADIENT_MODE, VideoLease  # noqa: E402
from live_adapter import GoveeController  # noqa: E402
from power_owner import PowerOwner  # noqa: E402
from test_live_adapter import LEFT, RIGHT, targets  # noqa: E402
from test_video_output import FakeProxy, FakeRelay  # noqa: E402
from video_output import VideoOutput  # noqa: E402


class ActiveVideoProfileTests(unittest.TestCase):
    def setUp(self):
        selected = targets()
        for target in selected:
            target["segment_count"] = 7 if GRADIENT_MODE else 0
        self.sent = []
        self.now = 10
        self.gate = PowerOwner(selected, clock=lambda: 10, wall_clock=lambda: 1000)
        self.controller = GoveeController(
            selected,
            sender=self.gate.wrap(
                lambda device, command, data: self.sent.append((device["device"], command)) or True
            ),
            # Video frames arrive over time; DreamView output is paced per lamp.
            clock=lambda: self.now,
            wall_clock=lambda: 1000,
            power_owner=self.gate,
        )
        self.controller.update_statuses(
            {key: ({"onOff": 1, "brightness": 65}, 10) for key in (LEFT, RIGHT)}
        )
        self.lease = VideoLease(clock=lambda: 10, wall_clock=lambda: 1000)
        self.output = VideoOutput(
            self.controller,
            FakeRelay(),
            [FakeProxy() for _ in range(4)],
            lease=self.lease,
            clock=lambda: 10,
        )

    def frame(self, sequence=1, black=False):
        # Longer than the first DreamView burst (mode, frame and brightness) takes
        # at the default 12 frames/s, so every frame finds a free slot.
        self.now += 0.3
        # Both lamps keep answering status polls between frames.
        self.controller.update_statuses(
            {key: ({"onOff": 1, "brightness": 65}, self.now) for key in (LEFT, RIGHT)}
        )
        rows = [[i, 0, 0, 0] for i in range(18 if GRADIENT_MODE else 14)]
        if not black:
            rows[0] = [0, 65535, 0, 0]
            rows[1] = [1, 0, 0, 65535]
            if GRADIENT_MODE:
                rows[14], rows[15] = [14, 0, 65535, 0], [15, 0, 0, 65535]
                rows[16], rows[17] = [16, 0, 65535, 0], [17, 65535, 0, 0]
        self.output.submit(
            {
                "action": "frame",
                "session": "archive-profile",
                "sequence": sequence,
                "issued_at": 1000,
                "colors": rows,
            }
        )
        self.assertTrue(self.output.process_once())
        self.output.render()

    def grant(self, sequence, **states):
        self.gate.submit(
            {"session": "home", "sequence": sequence, "issued_at": 1000, "states": states}
        )

    def test_profile_maps_expected_colors_to_only_selected_outputs_without_power(self):
        self.grant(1, left=True, right=True)
        self.frame()
        self.assertEqual({device for device, _ in self.sent}, {LEFT, RIGHT})
        self.assertFalse(any(command == "turn" for _, command in self.sent))
        left, right = self.controller.target("left"), self.controller.target("right")
        self.assertEqual(left["brightness"], 65)
        if GRADIENT_MODE:
            self.assertEqual(len(left["segments"]), 7)
            self.assertEqual(left["segments"][0], (255, 0, 0))
            self.assertEqual(left["segments"][3], (0, 255, 0))
            self.assertEqual(left["segments"][6], (0, 0, 255))
            self.assertEqual(right["segments"][0], (0, 0, 255))
            self.assertEqual(right["segments"][6], (255, 0, 0))
        else:
            self.assertEqual(left["rgb"], (255, 0, 0))
            self.assertEqual(right["rgb"], (0, 0, 255))

    def test_power_grant_is_required_and_right_off_blocks_fresh_black_frames(self):
        self.frame()
        self.assertEqual(self.sent, [])
        self.grant(1, left=True, right=True)
        self.frame(2)
        self.assertEqual({device for device, _ in self.sent}, {LEFT, RIGHT})
        self.grant(2, right=False)
        self.sent.clear()
        self.frame(3, black=True)
        self.assertEqual({device for device, _ in self.sent}, {LEFT})
        self.assertFalse(any(command == "turn" for _, command in self.sent))


if __name__ == "__main__":
    unittest.main()
