import base64
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "experiments/hue-bridge"))
from govee_segments import interpolate, segment_data  # noqa: E402


class SegmentTests(unittest.TestCase):
    def test_segment_frames_match_h6076_protocol_and_cover_only_seven_zones(self):
        colors = [
            (120, 0, 0),
            (100, 20, 0),
            (80, 40, 0),
            (0, 120, 0),
            (0, 80, 40),
            (0, 40, 80),
            (0, 0, 120),
        ]
        data = segment_data(colors)
        self.assertEqual(set(data), {"command"})
        self.assertEqual(len(data["command"]), 7)
        for index, encoded in enumerate(data["command"]):
            frame = base64.b64decode(encoded)
            self.assertEqual(len(frame), 20)
            self.assertEqual(frame[:4], bytes.fromhex("33 05 15 01"))
            self.assertEqual(frame[4:7], bytes(colors[index]))
            self.assertEqual(frame[7:12], bytes(5))
            self.assertEqual(frame[12:14], (1 << index).to_bytes(2, "little"))
            self.assertEqual(frame[14:19], bytes(5))
            checksum = 0
            for byte in frame:
                checksum ^= byte
            self.assertEqual(checksum, 0)

    def test_three_stops_keep_bottom_middle_top_and_interpolate_between(self):
        self.assertEqual(
            interpolate([(120, 0, 0), (0, 120, 0), (0, 0, 120)]),
            [
                (120, 0, 0),
                (80, 40, 0),
                (40, 80, 0),
                (0, 120, 0),
                (0, 80, 40),
                (0, 40, 80),
                (0, 0, 120),
            ],
        )

    def test_uniform_color_and_reversed_gradient(self):
        self.assertEqual(interpolate([(1, 2, 3)]), [(1, 2, 3)] * 7)
        stops = [(255, 0, 0), (0, 0, 255)]
        self.assertEqual(interpolate(stops, reverse=True), list(reversed(interpolate(stops))))

    def test_malformed_colors_never_produce_packets(self):
        for value in (
            [],
            [(0, 0, 0)] * 6,
            [(0, 0, 0)] * 8,
            [(0, 0, True)] * 7,
            [(-1, 0, 0)] * 7,
            [(256, 0, 0)] * 7,
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                segment_data(value)
        for value in ([], [(1, 2)], [(0, float("nan"), 0)]):
            with self.subTest(value=value), self.assertRaises(ValueError):
                interpolate(value)
