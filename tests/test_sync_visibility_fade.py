"""Soft Sync-only occlusion at the desk edge and its lateral penumbra."""
import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bridge'))
from sync_projection import (  # noqa: E402
    _segment_visibility,
    project_govee_frame,
    project_native_frame,
)
from test_sync_projection import frame, layout, sync_profile  # noqa: E402


class SyncVisibilityFadeTests(unittest.TestCase):
    def test_whole_smoothstep_band_has_half_gain_and_brightness_weighted_centroid(self):
        gain, center = _segment_visibility(70,85,85,15)
        self.assertAlmostEqual(gain,.5)
        self.assertAlmostEqual(center,80.5)

    def test_deep_play_is_black_and_near_play_fades_over_fifteen_centimeters(self):
        data = layout()
        for height, gain in ((40,0), (70,0), (75,7/27), (77.5,.5), (85,1), (100,1)):
            data['devices'][4]['positionCm']['height'] = height
            profile = sync_profile(data)
            with self.subTest(height=height):
                self.assertAlmostEqual(profile['play_gains']['play-center'], gain)
                raw = {channel:(60000,30000,15000) for channel in range(18)}
                self.assertEqual(project_native_frame(raw,profile)[12],
                                 tuple(round(value*gain) for value in raw[12]))

    def test_floor_zone_crossing_fade_has_partial_gain_and_lower_zones_stay_black(self):
        profile = sync_profile(layout())
        row = profile['govee']['left']
        self.assertEqual(row['gains'][:3], [0,0,0])
        self.assertGreater(row['gains'][3], 0)
        self.assertLess(row['gains'][3], 1)
        self.assertGreater(row['gains'][4], row['gains'][3])
        self.assertEqual(row['gains'][5:], [1,1])
        raw = {channel:(60000,30000,15000) for channel in range(18)}
        colors = project_govee_frame(raw,'left',profile)
        self.assertEqual(colors[:3], [(0,0,0)]*3)
        self.assertEqual(colors[3], tuple(round(v*row['gains'][3]) for v in raw[2]))

    def test_lateral_distance_broadens_fade_below_the_table_edge(self):
        data = layout()
        near = sync_profile(data)
        data['devices'][0]['positionCm']['x'] = 5
        far = sync_profile(data)
        self.assertEqual(near['govee']['left']['fade_cm'], 15)
        self.assertEqual(far['govee']['left']['fade_cm'], 30)
        self.assertGreater(far['govee']['left']['gains'][2],
                           near['govee']['left']['gains'][2])
        self.assertGreater(far['govee']['left']['gains'][3],
                           near['govee']['left']['gains'][3])
        data['devices'][1]['positionCm']['x'] = 195
        right = sync_profile(data)
        self.assertEqual(right['govee']['right']['gains'], far['govee']['left']['gains'])

    def test_desk_width_controls_spread_but_monitor_offset_does_not_move_the_desk(self):
        data = layout()
        before = sync_profile(data)
        data['desk']['width'] = 100
        narrower = sync_profile(data)
        self.assertGreater(narrower['govee']['left']['fade_cm'],
                           before['govee']['left']['fade_cm'])
        self.assertNotEqual(narrower['signature'], before['signature'])
        data['devices'][2]['positionCm']['x'] += 25
        shifted = sync_profile(data)
        self.assertEqual(shifted['govee']['left']['gains'],
                         narrower['govee']['left']['gains'])

    def test_raised_floor_has_full_gain_and_visibility_does_not_change_membership(self):
        data = layout()
        before = sync_profile(data)
        data['devices'][0]['positionCm']['height'] = 145
        profile = sync_profile(data)
        self.assertEqual(profile['govee']['left']['gains'], [1]*7)
        self.assertEqual(profile['enabled_roles'], before['enabled_roles'])
        self.assertEqual(profile['native_roles'], before['native_roles'])
        self.assertEqual(before['play_gains']['play-center'], 0)
        self.assertIn('play-center', before['native_roles'])

    def test_legacy_profiles_without_gains_keep_unit_gain(self):
        profile = sync_profile(layout())
        profile.pop('play_gains')
        for row in profile['govee'].values():
            row.pop('gains')
        raw = frame()
        original = copy.deepcopy(raw)
        projected = project_native_frame(raw,profile)
        self.assertNotEqual(projected[12], (0,0,0))
        self.assertEqual(raw, original)
