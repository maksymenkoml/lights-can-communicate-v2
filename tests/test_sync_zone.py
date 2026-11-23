"""Pure Sync zone geometry, validation and table-relative placement contracts."""

import copy
import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge"))
from sync_zone import default_zone, validate_zone, vertical_bounds, zone_points  # noqa: E402


def layout():
    return {
        "desk": {"width": 160, "height": 85},
        "screen": {"positionCm": {"x": 100, "height": 110},
                   "inches": math.hypot(80, 45) / 2.54},
        "devices": [
            {"id": key, "positionCm": {"x": x, "height": height}}
            for key, x, height in (
                ("govee-left", 10, 60), ("govee-right", 195, 60),
                ("hue-monitor", 100, 110), ("play-left", 60, 15),
                ("play-center", 100, 15), ("play-right", 140, 15),
            )
        ],
    }


def zone():
    return {"version": 1, "points": [
        {"u": -2, "h": 0}, {"u": 2, "h": 0},
        {"u": 2, "h": -95}, {"u": 1, "h": -15},
        {"u": -1, "h": -15}, {"u": -2, "h": -95},
    ]}


def world(points):
    return [{"x": x, "height": height} for x, height in points]


class SyncZoneTests(unittest.TestCase):
    def test_default_extents_and_lower_heights_preserve_existing_fade(self):
        data = layout()
        self.assertEqual(default_zone(data), world([
            (-30, 85), (230, 85), (230, 20), (180, 70), (20, 70), (-30, 20),
        ]))
        for x, lower in ((10, 60), (195, 55), (100, 70), (60, 70), (140, 70)):
            with self.subTest(x=x):
                self.assertEqual(vertical_bounds(default_zone(data), x), (lower, 85))

    def test_default_extents_round_outward_for_enabled_devices(self):
        data = layout()
        data["devices"][0]["positionCm"]["x"] = -61
        data["devices"][1]["positionCm"]["x"] = 251
        points = default_zone(data)
        self.assertEqual((points[0]["x"], points[1]["x"]), (-85, 275))
        data["devices"][0]["enabled"] = False
        data["devices"][1]["enabled"] = False
        points = default_zone(data)
        self.assertEqual((points[0]["x"], points[1]["x"]), (-30, 230))

    def test_default_monitor_width_counts_even_when_strip_is_disabled(self):
        data = layout()
        data["devices"][2].update(enabled=False)
        data["devices"][2]["positionCm"]["x"] = 251
        data["screen"]["positionCm"]["x"] = 251
        self.assertEqual(default_zone(data)[1]["x"], 315)

    def test_missing_or_null_zone_selects_fresh_automatic_points(self):
        data = layout()
        expected = default_zone(data)
        self.assertEqual(zone_points(data), expected)
        data["syncZone"] = None
        self.assertEqual(zone_points(data), expected)
        points = zone_points(data)
        points[0]["height"] = 999
        self.assertEqual(zone_points(data), expected)
        self.assertIsNone(validate_zone(None))

    def test_custom_zone_follows_table_resize_without_mutating_saved_data(self):
        data = layout()
        data["syncZone"] = zone()
        before = copy.deepcopy(data)
        self.assertEqual(zone_points(data), world([
            (-60, 85), (260, 85), (260, -10), (180, 70), (20, 70), (-60, -10),
        ]))
        self.assertEqual(data, before)
        data["desk"] = {"width": 200, "height": 100}
        self.assertEqual(zone_points(data), world([
            (-100, 100), (300, 100), (300, 5), (200, 85), (0, 85), (-100, 5),
        ]))
        points = zone_points(data)
        points[0]["x"] = 0
        self.assertEqual(data["syncZone"], before["syncZone"])

    def test_validation_returns_independent_canonical_points(self):
        data = zone()
        validated = validate_zone(data)
        self.assertEqual(validated, data)
        self.assertIsNot(validated, data)
        self.assertIsNot(validated["points"], data["points"])
        for first, second in zip(validated["points"], data["points"]):
            self.assertIsNot(first, second)
        validated["points"][0]["u"] = 0
        self.assertEqual(data["points"][0]["u"], -2)

    def test_tilted_concave_zone_uses_both_interpolated_envelopes(self):
        data = {"version": 1, "points": [
            {"u": -3, "h": 10}, {"u": 3, "h": 40},
            {"u": 3, "h": -40}, {"u": 1, "h": 5},
            {"u": -1, "h": -15}, {"u": -3, "h": -50},
        ]}
        validate_zone(data)
        points = world([(p["u"], p["h"]) for p in data["points"]])
        for x, expected in ((-3, (-50, 10)), (-2, (-32.5, 15)),
                            (-1, (-15, 20)), (0, (-5, 25)), (1, (5, 30)),
                            (2, (-17.5, 35)), (3, (-40, 40))):
            with self.subTest(x=x):
                self.assertEqual(vertical_bounds(points, x), expected)

    def test_sections_deduplicate_vertices_and_handle_pinched_extents(self):
        points = world([(-3, 0), (3, 0), (2, -3), (1, -1), (-1, -1), (-2, -3)])
        validate_zone({"version": 1, "points": [
            {"u": point["x"], "h": point["height"]} for point in points]})
        before = copy.deepcopy(points)
        self.assertEqual(vertical_bounds(points, -3), (0, 0))
        self.assertEqual(vertical_bounds(points, 3), (0, 0))
        self.assertEqual(vertical_bounds(points, -2), (-3, 0))
        self.assertEqual(vertical_bounds(points, 1), (-1, 0))
        self.assertIsNone(vertical_bounds(points, -3.001))
        self.assertIsNone(vertical_bounds(points, 3.001))
        self.assertEqual(points, before)

    def test_narrower_top_and_inclusive_coordinate_limits_are_valid(self):
        data = {"version": 1, "points": [
            {"u": 0, "h": 400}, {"u": 1, "h": 400},
            {"u": 20, "h": -400}, {"u": 2, "h": -1},
            {"u": -2, "h": -1}, {"u": -20, "h": -400},
        ]}
        self.assertEqual(validate_zone(data), data)
        points = world([(p["u"], p["h"]) for p in data["points"]])
        self.assertEqual(vertical_bounds(points, -20), (-400, -400))
        self.assertEqual(vertical_bounds(points, 20), (-400, -400))

    def test_minimum_top_separation_is_one_centimeter_including_extrapolation(self):
        data = {"version": 1, "points": [
            {"u": -1, "h": 0}, {"u": 1, "h": 10},
            {"u": 2, "h": 14}, {"u": 1, "h": 9},
            {"u": -1, "h": -1}, {"u": -2, "h": -6},
        ]}
        self.assertEqual(validate_zone(data), data)
        data["points"][2]["h"] = 14.01
        with self.assertRaises(ValueError):
            validate_zone(data)

    def test_rejects_malformed_schema_and_nonfinite_or_wrong_numeric_types(self):
        bad_values = [[], "private-secret", True, 1, {},
                      {**zone(), "private-secret": 1},
                      {**zone(), "version": True}, {**zone(), "version": 1.0},
                      {**zone(), "version": 2}, {"version": 1, "points": ()},
                      {"version": 1, "points": zone()["points"][:-1]}]
        for key, value in (("u", True), ("h", False), ("u", "private-secret"),
                           ("u", float("nan")), ("h", float("inf")),
                           ("h", -float("inf")), ("u", 20.01), ("u", -20.01),
                           ("h", 400.01), ("h", -400.01), ("h", 10**1000)):
            bad = zone()
            bad["points"][0][key] = value
            bad_values.append(bad)
        for point in (None, [], {"u": -2}, {"u": -2, "h": 0, "private-secret": 1}):
            bad = zone()
            bad["points"][0] = point
            bad_values.append(bad)
        for value in bad_values:
            with self.subTest(value=value):
                before = copy.deepcopy(value)
                with self.assertRaises(ValueError) as error:
                    validate_zone(value)
                self.assertNotIn("private-secret", str(error.exception))
                if not isinstance(value, dict) or not any(
                        isinstance(p, dict) and any(isinstance(n, float) and math.isnan(n)
                                                   for n in p.values())
                        for p in value.get("points", [])):
                    self.assertEqual(value, before)

    def test_rejects_duplicate_reversed_crossing_and_nonmonotone_points(self):
        bad_points = [
            [(-2, 0), (2, 0), (2, -95), (1, -15), (-1, -15), (-2, 0)],
            [(2, 0), (-2, 0), (2, -95), (1, -15), (-1, -15), (-2, -95)],
            [(-2, 0), (2, 0), (2, -95), (-1, -15), (1, -15), (-2, -95)],
            [(-2, 0), (2, 0), (2, -95), (1, 0), (-1, -15), (-2, -95)],
            [(2, 0), (3, 0), (1, -80), (0, -20), (-1, -20), (-2, -80)],
        ]
        for points in bad_points:
            with self.subTest(points=points):
                with self.assertRaises(ValueError):
                    validate_zone({"version": 1, "points": [
                        {"u": u, "h": h} for u, h in points]})


if __name__ == "__main__":
    unittest.main()
