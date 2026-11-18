"""Floor canonicalization and persisted table-relative Sync zone configuration."""
import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bridge'))
from desk_layout import validate_layout  # noqa: E402
from sync_projection import project_native_frame, sync_profile  # noqa: E402
from test_desk_layout import layout  # noqa: E402
from test_sync_projection import frame  # noqa: E402


def custom_zone():
    return {'version':1,'points':[{'u':u,'h':h} for u,h in
                                  ((-2,50),(2,50),(2,-50),(1,-50),(-1,-50),(-2,-50))]}


class FloorZoneContractTests(unittest.TestCase):
    def test_saved_body_anchors_are_canonicalized_above_floor(self):
        data = layout()
        for row in data['devices']:
            row['positionCm']['height'] = 0
        data['devices'][3]['orientation'] = 'vertical'
        result = validate_layout(data)
        heights = {row['id']:row['positionCm']['height'] for row in result['devices']}
        self.assertEqual(heights['govee-left'],60)
        self.assertEqual(heights['govee-right'],60)
        self.assertEqual(heights['play-left'],15)
        self.assertEqual(heights['play-center'],5)
        self.assertGreaterEqual(heights['hue-monitor'],25)

    def test_custom_zone_roundtrips_and_explicit_null_resets_to_auto(self):
        data = layout()
        data['syncZone'] = custom_zone()
        result = validate_layout(data)
        self.assertEqual(result['syncZone'],data['syncZone'])
        data['syncZone'] = None
        self.assertNotIn('syncZone',validate_layout(data))

    def test_table_has_only_dimensions_and_cannot_acquire_position_fields(self):
        for key,value in (('x',120),('y',-5),('positionCm',{'x':100,'height':0})):
            data = layout()
            data['desk'][key] = value
            with self.subTest(key=key),self.assertRaises(ValueError):
                validate_layout(data)

    def test_custom_zone_masks_strip_from_raw_palette_and_follows_table_height(self):
        data = layout()
        data['syncZone'] = custom_zone()
        profile = sync_profile(validate_layout(data))
        raw = frame()
        original = copy.deepcopy(raw)
        result = project_native_frame(raw,profile)
        self.assertTrue(any(result[key] != raw[key] for key in range(2,11)))
        self.assertEqual(raw,original)
        data['desk']['height'] += 10
        shifted = sync_profile(validate_layout(data))
        self.assertNotEqual(shifted['strip_gains'],profile['strip_gains'])


if __name__ == '__main__':
    unittest.main()
