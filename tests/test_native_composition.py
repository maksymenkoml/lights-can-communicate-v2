"""Native membership uses the authorized inventory and bridge-returned channel IDs."""

import copy
import struct
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "experiments" / "hue-bridge"))

from hue_entertainment_relay import HueEntertainmentRelay
from test_hue_entertainment_relay import FakeConnection
from test_native_profile import OTHER, profile_data
from test_native_profile_integration import stream_config, synthetic_area

from lights_can_communicate_v2_core.native_profile import NativeHueProfile

ROLES = ('hue_center', 'hue_left', 'hue_right', 'strip')


def composition(*roles):
    return {'native_roles': roles, 'enabled_roles': roles, 'signature': repr(roles)}


class NativeCompositionTests(unittest.TestCase):
    def setUp(self):
        self.profile = NativeHueProfile.from_dict(profile_data())
        self.area = synthetic_area()
        self.area['locations'] = {'service_locations': [
            {'service': {'rid': self.profile.expected_members[i][0], 'rtype': 'entertainment'},
             'positions': [{'x': 0.1 * i, 'y': 0, 'z': 0}]}
            for i in range(4)]}
        self.calls = []
        self.connections = []
        self.relay = HueEntertainmentRelay(stream_config(), requester=self.request,
                                           connection_factory=self.connect, profile=self.profile)

    def connect(self, _):
        result = FakeConnection()
        self.connections.append(result)
        return result

    def request(self, method, body=None):
        self.calls.append((method, copy.deepcopy(body)))
        if method == 'GET':
            return copy.deepcopy(self.area)
        if 'locations' in body:
            self.area['locations'] = copy.deepcopy(body['locations'])
            selected = {row['service']['rid'] for row in body['locations']['service_locations']}
            members = [(i, member) for i, member in enumerate(self.profile.expected_members)
                       if member[0] in selected]
            self.area['channels'] = [
                {'channel_id': 40 + n, 'members': [
                    {'service': {'rid': rid, 'rtype': 'entertainment'}, 'index': index}]}
                for n, (_, (rid, index)) in enumerate(reversed(members))]
            selected_roles = {ROLES[min(i, 3)] for i, _ in members}
            self.area['light_services'] = [
                {'rid': self.profile.light_ids[role], 'rtype': 'light'}
                for role in selected_roles]
        elif body == {'action': 'start'}:
            self.area.update(status='active', active_streamer={'rid': 'synthetic-stream-owner'})
        elif body == {'action': 'stop'}:
            self.area.update(status='inactive')
            self.area.pop('active_streamer', None)
        return {'errors': []}

    def test_subset_readback_reordered_ids_use_canonical_color_and_same_area(self):
        self.assertTrue(self.relay.start(composition('hue_left', 'strip')))
        frame = {i: (1000 + i, 2000 + i, 3000 + i) for i in range(14)}
        self.relay.send(frame)
        packet = self.connections[-1].frames[-1]
        self.assertEqual(packet[16:52], self.profile.area_id.encode())
        rows = [struct.unpack('!BHHH', packet[i:i + 7]) for i in range(52, len(packet), 7)]
        self.assertEqual(len(rows), 10)
        self.assertEqual(rows[0], (40, 1010, 2010, 3010))
        self.assertEqual(rows[-1], (49, 1011, 2011, 3011))
        self.assertEqual([r['service']['rid'] for r in self.area['locations']['service_locations']],
                         [self.profile.expected_members[i][0] for i in (1, 3)])
        self.relay.stop()
        self.assertFalse(self.relay.owned)

    def test_empty_subset_releases_old_area_without_empty_put_or_dtls(self):
        self.relay.start(composition('hue_left'))
        connection = self.connections[-1]
        count = len(self.connections)
        self.assertTrue(self.relay.start(composition()))
        self.assertTrue(connection.closed)
        self.assertFalse(self.relay.owned)
        self.assertEqual(len(self.connections), count)
        self.assertFalse(any(body == {'locations': {'service_locations': []}}
                             for _, body in self.calls))
        self.relay.send({i: (0, 0, 0) for i in range(14)})

    def test_foreign_active_area_never_stopped_or_reconfigured(self):
        self.area.update(status='active', active_streamer={'rid': 'foreign'})
        self.assertFalse(self.relay.start(composition('hue_left')))
        self.assertFalse(any(method == 'PUT' for method, _ in self.calls))

    def test_invalid_inventory_and_member_or_location_metadata_fail_before_write(self):
        original = copy.deepcopy(self.area)
        for mutate in (
            lambda a: a['light_services'][0].update(rid=OTHER),
            lambda a: a['light_services'].append(a['light_services'][0]),
            lambda a: a['channels'][0]['members'][0]['service'].update(rtype='light'),
            lambda a: a['channels'][0]['members'][0].update(index=True),
            lambda a: a['channels'].append(a['channels'][0]),
            lambda a: a['locations']['service_locations'][0]['service'].update(rid=OTHER),
            lambda a: a['locations']['service_locations'].append(
                a['locations']['service_locations'][0]),
        ):
            self.area = copy.deepcopy(original)
            self.calls.clear()
            mutate(self.area)
            with self.assertRaises(ValueError):
                self.relay.start(composition('hue_left'))
            self.assertFalse(any(method == 'PUT' for method, _ in self.calls))

    def test_incorrect_readback_never_opens_dtls(self):
        original = self.request

        def request(method, body=None):
            result = original(method, body)
            if body and 'locations' in body:
                self.area['channels'][0]['members'][0]['index'] = 9
            return result

        self.relay.requester = request
        with self.assertRaises(ValueError):
            self.relay.start(composition('hue_left'))
        self.assertEqual(self.connections, [])

    def test_stop_validates_applied_selection_after_desired_membership_changes(self):
        self.relay.start(composition('hue_left'))
        self.relay.stop()
        self.assertTrue(self.connections[-1].closed)
        self.assertFalse(self.relay.owned)

    def test_foreign_stream_with_empty_selection_allows_govee_without_any_mutation(self):
        self.area.update(status='active', active_streamer={'rid': 'foreign'})
        self.assertTrue(self.relay.start(composition()))
        self.assertFalse(self.relay.owned)
        self.assertEqual(self.connections, [])
        self.assertFalse(any(method == 'PUT' for method, _ in self.calls))

    def test_rejoin_after_restart_can_restore_authorized_missing_service(self):
        self.relay.start(composition('hue_left'))
        self.relay.stop()
        self.relay = HueEntertainmentRelay(stream_config(), requester=self.request,
                                           connection_factory=self.connect, profile=self.profile)
        self.assertTrue(self.relay.start(composition('hue_right')))
        self.assertEqual({row['rid'] for row in self.area['light_services']},
                         {self.profile.light_ids['hue_right']})
        self.assertEqual(len(self.connections), 2)

    def test_location_update_preserves_optional_equalization_and_omits_readonly_position(self):
        for row in self.area['locations']['service_locations']:
            row['position'] = {'x': 0, 'y': 0, 'z': 0}
            row['equalization_factor'] = 0.8
        self.relay.start(composition('hue_left'))
        body = next(body for _, body in self.calls if body and 'locations' in body)
        row = body['locations']['service_locations'][0]
        self.assertEqual(set(row), {'service', 'positions', 'equalization_factor'})
        self.assertEqual(row['equalization_factor'], 0.8)

    def test_recover_observed_owned_subset_ignores_new_desired_selection(self):
        self.relay.start(composition('hue_left'))
        self.relay.connection.close()
        self.relay = HueEntertainmentRelay(stream_config(), requester=self.request,
                                           connection_factory=self.connect, profile=self.profile)
        self.relay.layout_store = type(
            'Layout', (), {'sync_profile': lambda _: composition('strip')})()
        self.relay.recover()
        self.assertEqual(self.area['status'], 'inactive')
        self.assertFalse(self.relay.owned)

    def test_inactive_readback_with_stale_owner_never_opens_dtls(self):
        original = self.request

        def request(method, body=None):
            result = original(method, body)
            if body == {'action': 'start'}:
                self.area['status'] = 'inactive'
            return result

        self.relay.requester = request
        with self.assertRaises(ValueError):
            self.relay.start(composition('hue_left'))
        self.assertEqual(self.connections, [])

    def foreign_after_own_stop(self, selected):
        self.relay.start(composition('hue_left'))
        existing_connections = len(self.connections)
        original = self.request
        reads_after_stop = 0
        stopped = False
        foreign_read_end = None

        def request(method, body=None):
            nonlocal reads_after_stop, stopped, foreign_read_end
            if method == 'GET' and stopped:
                reads_after_stop += 1
                if reads_after_stop == 2:
                    self.area.update(status='active', active_streamer={'rid': 'foreign'})
            result = original(method, body)
            if body == {'action': 'stop'}:
                stopped = True
            if method == 'GET' and reads_after_stop == 2:
                foreign_read_end = len(self.calls)
            return result

        self.relay.requester = request
        result = self.relay.start(composition(*selected))
        self.assertEqual(result, not selected)
        self.assertIsNotNone(foreign_read_end)
        self.assertFalse(any(method == 'PUT' for method, _ in self.calls[foreign_read_end:]))
        self.assertEqual(len(self.connections), existing_connections)
        self.assertTrue(self.connections[-1].closed)
        self.assertEqual(self.area['active_streamer'], {'rid': 'foreign'})
        self.assertFalse(self.relay.owned)

    def test_foreign_after_own_stop_blocks_same_membership_restart(self):
        self.foreign_after_own_stop(('hue_left',))

    def test_foreign_after_own_stop_blocks_changed_membership_location_write(self):
        self.foreign_after_own_stop(('hue_right',))

    def test_foreign_after_own_stop_with_empty_selection_keeps_govee_and_never_stops_foreign(self):
        self.foreign_after_own_stop(())

    def test_equalization_survives_subset_and_rejoin(self):
        for row in self.area['locations']['service_locations']:
            row['equalization_factor'] = 0.8
        self.relay.start(composition('hue_left'))
        self.relay.stop()
        self.relay.start(composition('hue_right'))
        row = self.area['locations']['service_locations'][0]
        self.assertEqual(row['service']['rid'], self.profile.expected_members[2][0])
        self.assertEqual(row.get('equalization_factor'), 0.8)
        self.assertEqual(row['positions'], [{'x': 0.2, 'y': 0, 'z': 0}])

    def test_invalid_equalization_rejected_before_any_membership_write(self):
        for invalid in (True, False, None, -0.1, 1.1, float('nan'), float('inf'), '0.8'):
            self.calls.clear()
            self.area['locations']['service_locations'][0]['equalization_factor'] = invalid
            with self.subTest(factor=invalid), self.assertRaises(ValueError):
                self.relay.start(composition('hue_left'))
            self.assertFalse(any(method == 'PUT' for method, _ in self.calls))

    def test_restart_cache_preserves_excluded_location_and_equalization(self):
        for row in self.area['locations']['service_locations']:
            row['equalization_factor'] = 0.8
        with TemporaryDirectory(prefix='tmp_native_locations_') as folder:
            path = Path(folder) / 'native-location-cache.json'
            self.relay = HueEntertainmentRelay(
                stream_config(), requester=self.request, connection_factory=self.connect,
                profile=self.profile, location_cache_file=path)
            self.assertFalse(path.exists())
            self.relay.start(composition('hue_left'))
            self.relay.stop()
            self.assertTrue(path.is_file())
            self.relay = HueEntertainmentRelay(
                stream_config(), requester=self.request, connection_factory=self.connect,
                profile=self.profile, location_cache_file=path)
            self.assertTrue(self.relay.start(composition('hue_right')))
            row = self.area['locations']['service_locations'][0]
            self.assertEqual(row['service']['rid'], self.profile.expected_members[2][0])
            self.assertEqual(row['positions'], [{'x': 0.2, 'y': 0, 'z': 0}])
            self.assertEqual(row['equalization_factor'], 0.8)

    def test_configured_cache_missing_role_metadata_fails_before_location_write(self):
        self.relay.start(composition('hue_left'))
        self.relay.stop()
        connections = len(self.connections)
        self.calls.clear()
        with TemporaryDirectory(prefix='tmp_native_locations_') as folder:
            self.relay = HueEntertainmentRelay(
                stream_config(), requester=self.request, connection_factory=self.connect,
                profile=self.profile, location_cache_file=Path(folder) / 'locations.json')
            with self.assertRaises(ValueError):
                self.relay.start(composition('hue_right'))
        self.assertFalse(any(method == 'PUT' for method, _ in self.calls))
        self.assertEqual(len(self.connections), connections)

    def test_cache_persistence_failure_prevents_membership_write(self):
        with TemporaryDirectory(prefix='tmp_native_locations_') as folder:
            self.relay = HueEntertainmentRelay(
                stream_config(), requester=self.request, connection_factory=self.connect,
                profile=self.profile, location_cache_file=Path(folder) / 'locations.json')
            with patch.object(self.relay.location_cache, 'merge',
                              side_effect=OSError('Native location cache unavailable')):
                with self.assertRaises(OSError):
                    self.relay.start(composition('hue_left'))
        self.assertFalse(any(method == 'PUT' for method, _ in self.calls))
        self.assertEqual(self.connections, [])

    def start_cached_subset(self, folder):
        path = Path(folder) / 'locations.json'
        for row in self.area['locations']['service_locations']:
            row['equalization_factor'] = 0.8
        self.relay = HueEntertainmentRelay(
            stream_config(), requester=self.request, connection_factory=self.connect,
            profile=self.profile, location_cache_file=path)
        self.relay.start(composition('hue_left'))
        self.calls.clear()
        self.area['locations']['service_locations'][0]['equalization_factor'] = 0.9
        return path

    def test_stop_releases_owned_stream_even_when_changed_metadata_cannot_be_saved(self):
        with TemporaryDirectory(prefix='tmp_native_locations_') as folder:
            self.start_cached_subset(folder)
            with patch('native_location_cache.os.replace', side_effect=OSError('disk failed')):
                self.relay.stop()
            self.assertFalse(self.relay.owned)
            self.assertEqual(self.area['status'], 'inactive')
            self.assertEqual([body for method, body in self.calls if method == 'PUT'],
                             [{'action': 'stop'}])
            self.assertTrue(self.connections[-1].closed)

    def test_recover_releases_owned_stream_without_persisting_changed_metadata(self):
        with TemporaryDirectory(prefix='tmp_native_locations_') as folder:
            path = self.start_cached_subset(folder)
            self.relay.connection.close()
            self.relay = HueEntertainmentRelay(
                stream_config(), requester=self.request, connection_factory=self.connect,
                profile=self.profile, location_cache_file=path,
                layout_store=type('Layout', (), {'sync_profile': lambda _: composition('strip')})())
            with patch('native_location_cache.os.replace', side_effect=OSError('disk failed')):
                self.relay.recover()
            self.assertFalse(self.relay.owned)
            self.assertEqual(self.area['status'], 'inactive')
            self.assertEqual([body for method, body in self.calls if method == 'PUT'],
                             [{'action': 'stop'}])

    def test_failed_start_readback_persistence_still_stops_our_started_stream(self):
        with TemporaryDirectory(prefix='tmp_native_locations_') as folder:
            self.start_cached_subset(folder)
            self.area['locations']['service_locations'][0]['equalization_factor'] = 0.8
            self.relay.stop()
            self.calls.clear()
            original = self.request

            def request(method, body=None):
                result = original(method, body)
                if body == {'action': 'start'}:
                    self.area['locations']['service_locations'][0]['equalization_factor'] = 0.9
                return result

            self.relay.requester = request
            with patch('native_location_cache.os.replace', side_effect=OSError('disk failed')):
                with self.assertRaises(OSError):
                    self.relay.start(composition('hue_left'))
            self.assertFalse(self.relay.owned)
            self.assertEqual(self.area['status'], 'inactive')
            self.assertEqual([body for method, body in self.calls if method == 'PUT'],
                             [{'action': 'start'}, {'action': 'stop'}])

    def test_cache_failure_cleanup_never_stops_foreign_owner(self):
        with TemporaryDirectory(prefix='tmp_native_locations_') as folder:
            self.start_cached_subset(folder)
            self.area['active_streamer'] = {'rid': 'foreign'}
            with patch('native_location_cache.os.replace', side_effect=OSError('disk failed')):
                self.relay.stop()
            self.assertFalse(self.relay.owned)
            self.assertEqual(self.area['active_streamer'], {'rid': 'foreign'})
            self.assertFalse(any(method == 'PUT' for method, _ in self.calls))

    def test_cleanup_validation_still_rejects_changed_membership(self):
        with TemporaryDirectory(prefix='tmp_native_locations_') as folder:
            self.start_cached_subset(folder)
            self.area['channels'][0]['members'][0]['index'] = 8
            with patch('native_location_cache.os.replace', side_effect=OSError('disk failed')):
                with self.assertRaises(ValueError):
                    self.relay.stop()
            self.assertTrue(self.relay.owned)
            self.assertFalse(any(method == 'PUT' for method, _ in self.calls))
