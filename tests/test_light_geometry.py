"""Zone geometry of one layout row on the front wall."""

import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge"))
from desk_layout import ROLES  # noqa: E402
from light_geometry import GEOMETRIES, geometry_for, segment_spans, zone_points  # noqa: E402

SCREEN = {"positionCm": {"x": 100, "height": 110}, "inches": math.hypot(80, 45) / 2.54}


def row(identifier, x=100, height=60, **fields):
    return {"id": identifier, "positionCm": {"x": x, "height": height}, **fields}


class LightGeometryTests(unittest.TestCase):
    def test_h6076_entry_holds_the_floor_lamp_constants(self):
        self.assertEqual(GEOMETRIES["H6076"], {
            "kind": "segmented-floor", "zones": 7, "anchor_offset_cm": 60,
            "first_cm": 6, "span_cm": 128,
        })

    def test_every_role_row_has_a_geometry(self):
        for identifier in ROLES:
            self.assertIn(geometry_for(row(identifier))["kind"],
                          {"segmented-floor", "screen", "point"})

    def test_geometry_for_roles_and_models(self):
        for identifier in ("govee-left", "govee-right"):
            self.assertEqual(geometry_for(row(identifier)), GEOMETRIES["H6076"])
        self.assertEqual(geometry_for(row("hue-monitor")), {"kind": "screen"})
        for identifier in ("play-left", "play-center", "play-right"):
            self.assertEqual(geometry_for(row(identifier)), {"kind": "point"})
        device = "device-" + "a" * 32
        self.assertEqual(geometry_for(row(device, model="H6076")), GEOMETRIES["H6076"])
        self.assertEqual(geometry_for(row(device, model="X1")), {"kind": "point"})
        self.assertEqual(geometry_for(row("virtual-spare", model="H6076")), GEOMETRIES["H6076"])
        self.assertEqual(geometry_for(row("virtual-spare")), {"kind": "point"})
        # A role row never takes its geometry from a model field.
        self.assertEqual(geometry_for(row("play-left", model="H6076")), {"kind": "point"})

    def test_zone_points_segmented_floor_matches_segment_centres(self):
        lamp = row("govee-left", x=20, height=60)
        points = zone_points(lamp, SCREEN)
        self.assertEqual(len(points), 7)
        for index, (x, height) in enumerate(points):
            self.assertEqual(x, 20)
            self.assertAlmostEqual(height, 6 + (index + .5) * 128 / 7)
        spans = segment_spans(lamp)
        self.assertEqual([(low + high) / 2 for low, high in spans], [h for _, h in points])
        self.assertEqual(spans[0][0], 6)
        self.assertAlmostEqual(spans[-1][1], 134)
        # The anchor is 60 cm above the base: raising the anchor raises every zone.
        raised = zone_points(row("govee-left", x=20, height=110), SCREEN)
        for (_, high), (_, low) in zip(raised, points):
            self.assertAlmostEqual(high - 50, low)

    def test_screen_zones_are_the_nine_anchors_of_the_monitor_strip(self):
        width = SCREEN["inches"] * 2.54 * 16 / math.hypot(16, 9)
        height = width * 9 / 16
        points = zone_points(row("hue-monitor", x=100, height=110), SCREEN)
        self.assertEqual(len(points), 9)
        self.assertAlmostEqual(points[0][0], 100 - width / 2)
        self.assertAlmostEqual(points[0][1], 110 - height / 2)
        self.assertAlmostEqual(points[4][0], 100)
        self.assertAlmostEqual(points[4][1], 110 + height / 2)
        self.assertAlmostEqual(points[8][0], 100 + width / 2)
        self.assertAlmostEqual(points[8][1], 110 - height / 2)
        # The screen position comes from the screen, not from the row.
        moved = zone_points(row("hue-monitor", x=0, height=0), SCREEN)
        self.assertEqual(moved, points)

    def test_point_lights_have_one_zone_at_their_position(self):
        self.assertEqual(zone_points(row("play-left", x=70, height=40), SCREEN), [(70, 40)])
        self.assertEqual(zone_points(row("device-" + "b" * 32, model="X1", x=5, height=15),
                                     SCREEN), [(5, 15)])
        self.assertEqual(segment_spans(row("play-left")), [])


if __name__ == "__main__":
    unittest.main()
