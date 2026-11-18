"""Backward-compatible layout membership and honest selected-output telemetry."""
import copy
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'experiments/hue-bridge'))
from desk_layout import LayoutStore, validate_layout  # noqa: E402
from test_desk_layout import layout  # noqa: E402
from test_live_preview import metadata  # noqa: E402
from test_setup_studio import studio  # noqa: E402

from lights_can_communicate_v2_core.live_preview import OutputPreviewStore, build_preview  # noqa: E402


class LayoutCompositionTests(unittest.TestCase):
    def test_optional_membership_roundtrips_false_and_omits_legacy_true(self):
        data = layout()
        data['devices'][0]['enabled'] = True
        data['devices'][4]['enabled'] = False
        result = validate_layout(data)
        self.assertNotIn('enabled', result['devices'][0])
        self.assertIs(result['devices'][4]['enabled'], False)
        for invalid in (0, 1, None, 'false'):
            data['devices'][4]['enabled'] = invalid
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_layout(data)

    def test_store_recomputes_membership_and_projection_without_losing_geometry(self):
        with TemporaryDirectory(prefix='tmp_composition_contract_') as folder:
            store = LayoutStore(Path(folder) / 'layout.json')
            self.assertTrue(store.enabled('hue_center'))
            self.assertIsNone(store.sync_profile())
            data = layout()
            store.save(data)
            before = store.sync_profile()
            data['devices'][4]['enabled'] = False
            store.save(data)
            self.assertFalse(store.enabled('hue_center'))
            self.assertFalse(store.enabled('play-center'))
            self.assertNotIn('hue_center', store.sync_profile()['native_roles'])
            self.assertEqual(store.snapshot()['devices'][4]['positionCm'],
                             data['devices'][4]['positionCm'])
            self.assertNotEqual(store.sync_profile()['signature'], before['signature'])

    def test_sync_role_own_roundtrips_and_sync_is_never_stored(self):
        data = layout()
        data['version'] = 4
        data['devices'][0]['syncRole'] = 'sync'
        data['devices'][4]['syncRole'] = 'own'
        result = validate_layout(data)
        self.assertEqual(result['version'], 4)
        self.assertNotIn('syncRole', result['devices'][0])
        self.assertEqual(result['devices'][4]['syncRole'], 'own')
        self.assertEqual(validate_layout(copy.deepcopy(result)), result)
        for invalid in ('off', 'Own', '', None, True, 1, ['own'], {'own': True}):
            data['devices'][4]['syncRole'] = invalid
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_layout(data)

    def test_version_three_rows_upgrade_without_a_sync_role(self):
        data = layout()
        data['devices'][4]['enabled'] = False
        result = validate_layout(data)
        self.assertEqual(result['version'], 4)
        self.assertTrue(all('syncRole' not in row for row in result['devices']))
        self.assertIs(result['devices'][4]['enabled'], False)

    def test_own_light_is_excluded_like_a_removed_row_but_keeps_its_place(self):
        with TemporaryDirectory(prefix='tmp_composition_own_') as folder:
            store = LayoutStore(Path(folder) / 'layout.json')
            data = layout()
            store.save(data)
            before = store.sync_profile()
            data['devices'][4]['syncRole'] = 'own'
            data['devices'][0]['syncRole'] = 'own'
            store.save(data)
            profile = store.sync_profile()
            self.assertFalse(store.enabled('hue_center'))
            self.assertFalse(store.enabled('play-center'))
            self.assertFalse(store.enabled('left'))
            self.assertTrue(store.enabled('hue_left'))
            self.assertEqual(profile['enabled_roles'], ['right', 'strip', 'hue_left', 'hue_right'])
            self.assertEqual(profile['native_roles'], ['strip', 'hue_left', 'hue_right'])
            removed = layout()
            removed['devices'][4]['enabled'] = False
            removed['devices'][0]['enabled'] = False
            store.save(removed)
            self.assertEqual(store.sync_profile()['enabled_roles'], profile['enabled_roles'])
            self.assertEqual(store.sync_profile()['native_roles'], profile['native_roles'])
            store.save(data)
            self.assertEqual(store.snapshot()['devices'][4]['positionCm'],
                             data['devices'][4]['positionCm'])
            self.assertNotEqual(profile['signature'], before['signature'])

    def test_sparse_native_observer_records_only_complete_selected_roles(self):
        store = OutputPreviewStore()
        store.record_native_channels({1:(65535,0,0), 3:(0,65535,0)})
        value = store.snapshot()
        self.assertEqual(value['play-left']['colors'], [[255,0,0]])
        self.assertNotIn('play-center', value)
        self.assertNotIn('hue-monitor', value)

    def test_existing_invalid_layout_does_not_reenable_inventory_on_cold_start(self):
        with TemporaryDirectory(prefix='tmp_invalid_composition_') as folder:
            path = Path(folder) / 'layout.json'
            path.write_text('{broken')
            self.assertFalse(LayoutStore(path).enabled('hue_center'))

    def test_excluded_native_stream_cannot_glow_or_latch_foreign_state(self):
        data = metadata()
        data['composition'] = {'enabled_roles':['left','right','strip','hue_left','hue_right']}
        data['hue_play']['hue_center']['native_mode'] = 'streaming'
        outputs = {'play-center':{'colors':[[255,0,0]], 'color_source':'scene-output',
                                  'source':'scene-output','brightness':100}}
        preview = build_preview(data, outputs)
        self.assertEqual(preview['mode'], 'scene')
        self.assertEqual(preview['lamps']['play-center']['colors'], [])
        self.assertIs(preview['lamps']['play-center']['on'], True)
        data['preview'] = copy.deepcopy(preview)
        public = studio.project_status(data, source_active=False)
        self.assertEqual(public['sync']['state'], 'off')
        self.assertEqual(public['lamps']['play-center']['colors'], [])


if __name__ == '__main__':
    unittest.main()
