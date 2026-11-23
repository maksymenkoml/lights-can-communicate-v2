"""Pure layout projection contracts with hand-derived screen samples."""

import copy
import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge"))
import sync_projection  # noqa: E402
from sync_projection import (  # noqa: E402
    _sample,
    light_entry,
    project_govee_frame,
    project_light_frame,
    project_native_frame,
)


def current(data):
    """Return a hand-written layout in the shape the projection reads (version 7).

    The screen object comes from the ``hue-monitor`` row and ``monitorInches``, as when a
    stored file loads. Nothing is rounded to the grid, so tests may use any height.
    """
    result = copy.deepcopy(data)
    monitor = next(row for row in result["devices"] if row["id"] == "hue-monitor")
    result["screen"] = {"positionCm": copy.deepcopy(monitor["positionCm"]),
                        "inches": result.pop("monitorInches")}
    result["version"] = 7
    return result


def sync_profile(data):
    return sync_projection.sync_profile(current(data))


def scene_fade(data):
    return sync_projection.scene_fade(current(data))


def layout():
    # An 80 by 45 cm screen makes the physical-coordinate expectations simple.
    return {
        "version": 3,
        "room": "Office",
        "desk": {"width": 160, "height": 85},
        "monitorInches": math.hypot(80, 45) / 2.54,
        "devices": [
            {"id": key, "positionCm": {"x": x, "height": height}, "scale": 100}
            for key, x, height in (
                ("govee-left", 20, 60),
                ("govee-right", 180, 60),
                ("hue-monitor", 100, 110),
                ("play-left", 70, 40),
                ("play-center", 100, 40),
                ("play-right", 130, 40),
            )
        ],
    }


def frame(size=18):
    return {channel: (channel * 1000, channel * 2000, channel * 3000)
            for channel in range(size)}


class SyncProjectionTests(unittest.TestCase):
    def test_lights_map_covers_composed_roles_only(self):
        data = layout()
        profile = sync_profile(data)
        self.assertEqual(list(profile["lights"]), [
            "govee-left", "govee-right", "hue-monitor", "play-left", "play-center", "play-right"])
        self.assertEqual({key: (len(entry["samples"]), len(entry["gains"]))
                          for key, entry in profile["lights"].items()},
                         {"govee-left": (7, 7), "govee-right": (7, 7), "hue-monitor": (9, 9),
                          "play-left": (1, 1), "play-center": (1, 1), "play-right": (1, 1)})
        data["devices"][1]["enabled"] = False
        data["devices"][4]["syncRole"] = "own"
        data["devices"][5]["syncRole"] = "off"
        profile = sync_profile(data)
        self.assertEqual(list(profile["lights"]),
                         ["govee-left", "hue-monitor", "play-left"])
        self.assertEqual(profile["enabled_roles"], ["left", "hue-monitor", "play-left"])
        # The legacy keys stay derived from the same geometry for every role row.
        self.assertEqual(profile["govee"]["left"]["samples"],
                         profile["lights"]["govee-left"]["samples"])
        self.assertEqual(profile["govee"]["left"]["gains"],
                         profile["lights"]["govee-left"]["gains"])
        self.assertEqual(profile["strip_gains"], profile["lights"]["hue-monitor"]["gains"])
        self.assertEqual([profile["plays"]["play-left"]], profile["lights"]["play-left"]["samples"])
        self.assertEqual([profile["play_gains"]["play-left"]],
                         profile["lights"]["play-left"]["gains"])
        self.assertIn("right", profile["govee"])

    def test_lights_map_includes_composed_devices_not_drawings(self):
        data = layout()
        device = {"id": "device-" + "a" * 32, "scale": 80,
                  "positionCm": {"x": 230, "height": 120}, "orientation": "horizontal",
                  "name": "Extra", "model": "X", "shape": "bar", "zones": 1}
        drawing = {"id": "virtual-sketch", "scale": 80,
                   "positionCm": {"x": -30, "height": 120}, "orientation": "horizontal",
                   "name": "Sketch", "shape": "bar"}
        excluded = dict(device, id="device-" + "b" * 32, syncRole="own")
        data["devices"] += [device, drawing, excluded]
        profile = sync_profile(data)
        self.assertIn(device["id"], profile["lights"])
        self.assertNotIn(drawing["id"], profile["lights"])
        self.assertNotIn(excluded["id"], profile["lights"])
        self.assertNotIn("fade_cm", profile["lights"][device["id"]])
        self.assertIn("fade_cm", profile["lights"]["govee-left"])
        frame = {channel: (channel * 1000, 0, 0) for channel in range(2, 14)}
        self.assertEqual(len(project_light_frame(frame, device["id"], profile)), 1)
        self.assertIn("play-center", profile["plays"])

    def test_screen_anchors_hit_capture_channels(self):
        raw = frame()
        for inches, x, height in ((32, 100, 110), (43, 60, 135), (55, 105, 40), (19.5, -35, 120),
                                  (math.hypot(80, 45) / 2.54, 100, 110)):
            data = layout()
            data["monitorInches"] = inches
            data["devices"][2]["positionCm"] = {"x": x, "height": height}
            profile = sync_profile(data)
            entry = profile["lights"]["hue-monitor"]
            with self.subTest(inches=inches, x=x, height=height):
                self.assertEqual([_sample(raw, point) for point in entry["samples"]],
                                 [raw[channel] for channel in range(2, 11)])
                self.assertEqual(entry["gains"], profile["strip_gains"])

    def test_project_light_frame_hidden_zones_black(self):
        data = layout()
        profile = sync_profile(data)
        raw = frame()
        left = project_light_frame(raw, "govee-left", profile)
        self.assertEqual(left, project_govee_frame(raw, "left", profile))
        self.assertEqual(left[:3], [(0, 0, 0)] * 3)
        self.assertNotEqual(left[3], (0, 0, 0))
        # A fully hidden light is black without an error.
        data["desk"]["height"] = 300
        data["syncZone"] = {"version": 1, "points": [
            {"u": -2, "h": 0}, {"u": 2, "h": 0}, {"u": 2, "h": -5}, {"u": 1, "h": -5},
            {"u": -1, "h": -5}, {"u": -2, "h": -5.5}]}
        hidden = sync_profile(data)
        self.assertEqual(hidden["lights"]["govee-left"]["samples"], [None] * 7)
        self.assertEqual(project_light_frame(raw, "govee-left", hidden), [(0, 0, 0)] * 7)
        self.assertEqual(project_govee_frame(raw, "left", hidden), [(0, 0, 0)] * 7)
        with self.assertRaises(ValueError):
            project_light_frame(raw, "govee-unknown", profile)
        with self.assertRaises(ValueError):
            project_light_frame({}, "govee-left", profile)

    def test_native_projection_matches_light_projection_of_the_same_light(self):
        profile = sync_profile(layout())
        raw = frame()
        native = project_native_frame(raw, profile)
        for key, channel in (("play-left", 11), ("play-center", 12), ("play-right", 13)):
            self.assertEqual([native[channel]], project_light_frame(raw, key, profile))
        self.assertEqual([native[channel] for channel in range(2, 11)],
                         project_light_frame(raw, "hue-monitor", profile))

    def test_generic_point_light(self):
        data = layout()
        extra = {"id": "device-" + "c" * 32, "model": "X1",
                 "positionCm": {"x": 70, "height": 120}, "scale": 100}
        entry = light_entry(extra, current(data))
        self.assertEqual(len(entry["samples"]), 1)
        self.assertEqual(len(entry["gains"]), 1)
        self.assertEqual(entry["gains"], [1])
        raw = frame()
        profile = {"lights": {extra["id"]: entry}}
        self.assertEqual(project_light_frame(raw, extra["id"], profile),
                         [_sample(raw, entry["samples"][0])])
        # The same placement as a Play light gives the same sample and gain.
        play = {"id": "play-left", "positionCm": extra["positionCm"]}
        self.assertEqual(light_entry(play, current(data)), entry)
        # A deep light is dark and a Govee model with segments gets its seven zones.
        deep = dict(extra, positionCm={"x": 70, "height": 20})
        self.assertEqual(light_entry(deep, current(data))["gains"], [0])
        floor = dict(extra, model="H6076", positionCm={"x": 50, "height": 60})
        self.assertEqual(len(light_entry(floor, current(data))["gains"]), 7)

    def test_default_floor_lamps_fade_below_desk_and_keep_upper_samples(self):
        profile = sync_profile(layout())
        for side, u in (("left", 0), ("right", 1)):
            self.assertEqual(profile["govee"][side]["visible"], [False] * 3 + [True] * 4)
            samples = profile["govee"][side]["samples"]
            self.assertEqual(samples[:3], [None] * 3)
            self.assertEqual(samples[3], (u, 0))
            self.assertGreater(samples[4][1], 11 / 630)
            self.assertLess(samples[4][1], 26 / 315)
            for point, v in zip(samples[5:], (89 / 210, 523 / 630)):
                self.assertAlmostEqual(point[0], u)
                self.assertAlmostEqual(point[1], v)

    def test_raising_base_above_desk_exposes_all_seven_segments(self):
        data = layout()
        data["devices"][0]["positionCm"]["height"] = 150
        profile = sync_profile(data)
        self.assertEqual(profile["govee"]["left"]["visible"], [True] * 7)
        self.assertTrue(all(point is not None for point in profile["govee"]["left"]["samples"]))
        self.assertEqual(profile["govee"]["left"]["samples"][-1], (0, 1))

    def test_segment_that_ends_at_lower_fade_boundary_is_hidden(self):
        data = layout()
        data["desk"]["height"] = 6 + 4 * 128 / 7 + 15
        samples = sync_profile(data)["govee"]["left"]["samples"]
        self.assertEqual(samples[:4], [None] * 4)
        self.assertIsNotNone(samples[4])

    def test_monitor_size_and_position_change_samples_without_moving_capture_palette(self):
        data = layout()
        before = sync_profile(data)
        data["monitorInches"] *= 1.1
        larger = sync_profile(data)
        self.assertNotEqual(larger["govee"], before["govee"])
        data["devices"][2]["positionCm"]["height"] += 5
        moved = sync_profile(data)
        self.assertLess(moved["govee"]["left"]["samples"][4][1],
                        larger["govee"]["left"]["samples"][4][1])
        raw = frame()
        projected = project_native_frame(raw, moved)
        self.assertEqual({key: projected[key] for key in range(2, 11)},
                         {key: raw[key] for key in range(2, 11)})

    def test_deep_play_is_dark_and_near_play_orientation_has_no_effect(self):
        data = layout()
        before = sync_profile(data)
        self.assertEqual(before["plays"], {
            "play-left": (0.125, 0), "play-center": (0.5, 0), "play-right": (0.875, 0),
        })
        self.assertEqual(set(before['play_gains'].values()), {0})
        data["devices"][3]["positionCm"]["height"] = 80
        data["devices"][3]["orientation"] = "vertical"
        self.assertEqual(sync_profile(data)["plays"], before["plays"])
        vertical = sync_profile(data)
        self.assertGreater(vertical['play_gains']['play-left'], 0)
        data['devices'][3]['orientation'] = 'horizontal'
        self.assertEqual(sync_profile(data)['play_gains'], vertical['play_gains'])

    def test_interior_sample_uses_nearest_edge_and_outside_sample_clamps_to_corner(self):
        data = layout()
        data["devices"][3]["positionCm"] = {"x": 65, "height": 110}
        data["devices"][4]["positionCm"] = {"x": 200, "height": 200}
        plays = sync_profile(data)["plays"]
        self.assertEqual(plays["play-left"], (0, 0.5))
        self.assertEqual(plays["play-center"], (1, 1))

    def test_native_projection_interpolates_bottom_palette_from_original_frame(self):
        data = layout()
        for row in data['devices'][3:]:
            row['positionCm']['height'] = 85
        profile = sync_profile(data)
        raw = frame()
        original = copy.deepcopy(raw)
        projected = project_native_frame(raw, profile)
        self.assertEqual(projected[11], (6500, 13000, 19500))
        self.assertEqual(projected[12], (12000, 24000, 36000))
        self.assertEqual(projected[13], (11500, 23000, 34500))
        self.assertEqual(raw, original)
        self.assertIsNot(projected, raw)
        self.assertEqual(set(projected), set(raw))
        self.assertEqual(projected[14], raw[14])

    def test_govee_projection_interpolates_rgb16_without_brightness(self):
        profile = sync_profile(layout())
        profile["govee"]["left"]["samples"] = [
            None, (0, 0.25), (0, 0.75), (0.125, 1), (0.875, 1), (1, 0.25), (1, 0),
        ]
        profile['govee']['left']['gains'] = [1]*7
        raw = frame(14)
        self.assertEqual(project_govee_frame(raw, "left", profile), [
            (0, 0, 0), (2500, 5000, 7500), (3500, 7000, 10500),
            (4500, 9000, 13500), (7500, 15000, 22500),
            (9500, 19000, 28500), (10000, 20000, 30000),
        ])
        self.assertEqual(raw, frame(14))

    def test_disabled_roles_are_excluded_native_raw_colors_retained_and_govee_black(self):
        data = layout()
        for index in (0, 2, 3):
            data["devices"][index]["enabled"] = False
        profile = sync_profile(data)
        self.assertEqual(profile["enabled_roles"], ["right", "play-center", "play-right"])
        self.assertEqual(profile["native_roles"], ["play-center", "play-right"])
        raw = frame()
        self.assertEqual(project_native_frame(raw, profile)[11], raw[11])
        self.assertEqual(project_govee_frame(raw, "left", profile), [(0, 0, 0)] * 7)
        self.assertNotEqual(project_govee_frame(raw, "right", profile), [(0, 0, 0)] * 7)

    def test_signature_ignores_metadata_scale_orientation_and_row_order(self):
        data = layout()
        before = sync_profile(data)
        self.assertEqual(before["basis"], "screen-perimeter-v1")
        self.assertEqual(len(before["signature"]), 64)
        int(before["signature"], 16)
        data["room"] = "Renamed"
        for row in data["devices"]:
            row.update(scale=20, name="Renamed", orientation="vertical", enabled=True)
        data["devices"].reverse()
        self.assertEqual(sync_profile(data)["signature"], before["signature"])
        data["devices"][0]["positionCm"]["x"] -= 5
        self.assertNotEqual(sync_profile(data)["signature"], before["signature"])
        data = layout()
        data["devices"][0]["enabled"] = False
        self.assertNotEqual(sync_profile(data)["signature"], before["signature"])

    def test_malformed_frames_fail_closed_even_for_disabled_roles(self):
        profile = sync_profile(layout())
        profile["enabled_roles"] = []
        profile["native_roles"] = []
        bad_frames = [None, [], {key: rgb for key, rgb in frame().items() if key != 12}]
        for color in ((-1, 0, 0), (65536, 0, 0), (1.5, 0, 0), (True, 0, 0),
                      (0, 0), "RGB"):
            raw = frame()
            raw[17] = color
            bad_frames.append(raw)
        for raw in bad_frames:
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    project_native_frame(raw, profile)
                with self.assertRaises(ValueError):
                    project_govee_frame(raw, "left", profile)

    def test_unknown_govee_side_is_rejected(self):
        with self.assertRaises(ValueError):
            project_govee_frame(frame(), "extra", sync_profile(layout()))


if __name__ == "__main__":
    unittest.main()
