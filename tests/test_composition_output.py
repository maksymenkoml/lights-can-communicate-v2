"""Composition gates synthetic scene and Sync output without changing power."""

import copy
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bridge'))
import live_adapter as adapter  # noqa: E402
from entertainment_stream import CHANNELS, VideoLease  # noqa: E402
from hue_strip_proxy import HueStripProxy  # noqa: E402
from live_adapter import GoveeController  # noqa: E402
from test_entertainment_stream import envelope as legacy_envelope  # noqa: E402
from test_hue_strip_proxy import FakeHue  # noqa: E402
from test_live_adapter import LEFT, RIGHT, command, targets  # noqa: E402
from test_video_output import FakeProxy, FakeRelay  # noqa: E402
from video_output import VideoOutput  # noqa: E402

NATIVE = ('strip', 'hue_left', 'hue_center', 'hue_right')


def envelope(*args, **kwargs):
    message = legacy_envelope(*args, **kwargs)
    if message['action'] == 'frame':
        supplied = {row[0] for row in message['colors']}
        message['colors'].extend([index, 0, 0, 0] for index in sorted(CHANNELS - supplied))
    return message


class Layout:
    def __init__(self):
        self.roles = {'left', 'right', *NATIVE}
        self.geometry = 0
        self.reads = 0

    def enabled(self, role, fallback=True):
        return role in self.roles

    present = enabled

    def scale(self, role, fallback):
        return fallback

    def snapshot(self):
        return {}

    def sync_profile(self):
        self.reads += 1
        return {'signature': str(self.geometry) + str(sorted(self.roles)),
                'basis': 'screen-perimeter-v1', 'enabled_roles': sorted(self.roles),
                'native_roles': [role for role in NATIVE if role in self.roles],
                'plays': {role: (0, 1) for role in NATIVE if role != 'strip'},
                'govee': {side: {'samples': [None, None, (0, 1), (0, 1), (0, 1),
                                            (1, 1), (1, 1)]} for side in ('left', 'right')}}


class CompositionOutputTests(unittest.TestCase):
    def setUp(self):
        self.now = 10
        self.layout = Layout()
        self.sent = []
        devices = targets()
        for device in devices:
            device['segment_count'] = 7
        self.controller = GoveeController(
            devices, sender=lambda device, name, data: self.sent.append(
                (device['side'], name, copy.deepcopy(data))) or True,
            clock=lambda: self.now, wall_clock=lambda: 1000, layout_store=self.layout)
        self.controller.update_statuses({key: ({'onOff': 1, 'brightness': 65}, self.now)
                                         for key in (LEFT, RIGHT)})
        self.proxies = []
        for role in NATIVE:
            proxy = FakeProxy()
            proxy.role = role
            self.proxies.append(proxy)
        self.relay = FakeRelay()
        original = self.relay.start
        self.relay.profiles = []

        def start(profile=None):
            self.relay.profiles.append(copy.deepcopy(profile))
            return original()

        self.relay.start = start
        self.lease = VideoLease(clock=lambda: self.now, wall_clock=lambda: 1000)
        self.output = VideoOutput(self.controller, self.relay, self.proxies,
                                  lease=self.lease, clock=lambda: self.now)

    def test_scene_exclusion_clears_jobs_and_dedup_preserves_order_and_needs_fresh_rejoin(self):
        self.controller.submit(command())
        self.controller.render()
        self.assertTrue(self.sent)
        self.layout.roles.remove('left')
        self.sent.clear()
        self.controller.render()
        self.assertEqual(self.sent, [])
        self.assertNotIn('left', self.controller.jobs)
        self.assertNotIn('left', self.controller.last_sent)
        self.assertTrue(self.controller.submit(command(sequence=2)))
        self.assertNotIn('left', self.controller.jobs)
        self.layout.roles.add('left')
        self.controller.render()
        self.assertEqual(self.sent, [])
        self.assertFalse(self.controller.submit(command(sequence=1)))
        self.assertTrue(self.controller.submit(command(sequence=3)))
        self.controller.render()
        self.assertTrue(self.sent)

    def test_excluded_hue_skips_io_retains_latest_scene_and_restores_on_rejoin(self):
        client = FakeHue()
        proxy = HueStripProxy(client, layout_store=self.layout, wall_clock=lambda: 1000)
        self.layout.roles.remove('strip')
        proxy.submit(command(side='strip'))
        client.read = lambda: (_ for _ in ()).throw(AssertionError('Excluded native read'))
        self.assertFalse(proxy.process_once())
        self.assertTrue(proxy.restore)
        self.assertIsNotNone(proxy.desired)
        client.read = lambda: copy.deepcopy(client.native)
        self.layout.roles.add('strip')
        self.assertTrue(proxy.process_once())
        self.assertEqual(len(client.writes), 1)

    def test_sync_uses_one_profile_raw_projection_hidden_segments_and_single_calibration(self):
        colors = [[i, 65535 if i == 4 else 0, 0, 65535 if i == 8 else 0] for i in range(14)]
        self.lease.submit(envelope(colors=colors))
        self.assertTrue(self.output.process_once())
        self.assertEqual(self.layout.reads, 1)
        native = self.relay.frames[-1]
        self.assertEqual(native[11], (65535, 0, 0))
        target = self.controller.target('left')
        self.assertEqual(target['brightness'], 65)
        self.assertEqual(target['segments'][:2], [(0, 0, 0)] * 2)
        self.assertEqual(target['segments'][2], (255, 0, 0))
        self.assertEqual(target['segments'][-1], (0, 0, 255))

    def test_excluded_off_play_does_not_block_remaining_and_native_empty_allows_govee(self):
        self.layout.roles.remove('hue_left')
        self.proxies[1].value.update(healthy=False, native_on=False)
        self.lease.submit(envelope())
        self.assertTrue(self.output.process_once())
        self.assertNotIn('hue_left', self.relay.profiles[-1]['native_roles'])
        self.layout.roles.difference_update(NATIVE)
        self.lease.submit(envelope(sequence=2))
        self.assertTrue(self.output.process_once())
        self.assertEqual(self.relay.profiles[-1]['native_roles'], [])

    def test_membership_handoff_holds_lease_geometry_does_not_restart_and_rejoin_fresh(self):
        self.lease.submit(envelope())
        self.output.process_once()
        self.layout.geometry += 1
        self.lease.submit(envelope(sequence=2))
        self.assertTrue(self.output.process_once())
        self.assertEqual(self.relay.started, 1)
        self.layout.roles.remove('hue_left')
        self.lease.submit(envelope(sequence=3))
        self.assertTrue(self.output.process_once())
        self.assertEqual(self.relay.stopped, 1)
        self.assertEqual(self.relay.started, 2)
        self.assertFalse(any(p.value.get('restore_requested') for p in self.proxies))
        self.assertTrue(self.output.active())
        self.layout.roles.remove('left')
        self.lease.submit(envelope(sequence=4))
        self.output.process_once()
        self.assertNotIn('left', self.controller.jobs)
        self.layout.roles.add('left')
        black = [[i, 0, 0, 0] for i in range(14)]
        self.lease.submit(envelope(sequence=5, colors=black))
        self.output.process_once()
        self.assertEqual(self.controller.target('left')['segments'], [(0, 0, 0)] * 7)

    def test_only_native_membership_change_clears_off_session_block(self):
        self.lease.submit(envelope())
        self.output.process_once()
        self.proxies[1].value['native_on'] = False
        self.output.process_once()
        self.assertEqual(self.output.blocked_session, 'test-session')
        self.layout.geometry += 1
        self.lease.submit(envelope(sequence=2))
        self.assertFalse(self.output.process_once())
        self.assertEqual(self.output.blocked_session, 'test-session')
        self.layout.roles.remove('hue_left')
        self.lease.submit(envelope(sequence=3))
        self.assertTrue(self.output.process_once())
        self.assertIsNone(self.output.blocked_session)

    def test_composition_with_legacy_whole_color_driver_does_not_require_segments(self):
        for device in self.controller.targets:
            device['segment_count'] = 0
        colors = [[i, 65535 if i == 4 else 0, 0, 65535 if i == 8 else 0] for i in range(14)]
        self.lease.submit(envelope(colors=colors))
        self.assertTrue(self.output.process_once())
        target = self.controller.target('left')
        self.assertNotIn('segments', target)
        self.assertEqual(target['rgb'], (255, 0, 0))
        self.assertEqual(target['brightness'], 65)

    def test_main_14_channel_composition_enables_only_known_h6076_with_bounded_packets(self):
        devices = targets()
        devices[0]['sku'] = 'H6076'
        devices[1]['model'] = 'H6076'
        configured = self.startup_targets(devices, self.layout)
        self.assertEqual([device['segment_count'] for device in configured], [7, 7])
        self.assertEqual(
            [device['max_gradient_packets_per_second'] for device in configured], [2, 2])

    def test_main_preserves_declared_capability_and_budget_without_autoenabling_unknown(self):
        devices = targets()
        devices[0].update(segment_count=7, max_gradient_packets_per_second=3)
        devices[1]['sku'] = 'UNKNOWN'
        configured = self.startup_targets(devices, self.layout)
        self.assertEqual([device['segment_count'] for device in configured], [7, 0])
        self.assertEqual(configured[0]['max_gradient_packets_per_second'], 3)

    def test_main_without_layout_preserves_legacy_h6076_whole_color_mode(self):
        devices = targets()
        devices[0]['sku'] = 'H6076'
        devices[1]['model'] = 'H6076'
        layout = Layout()
        layout.sync_profile = lambda: None
        configured = self.startup_targets(devices, layout)
        self.assertEqual([device['segment_count'] for device in configured], [0, 0])
        self.assertTrue(all('max_gradient_packets_per_second' not in device
                            for device in configured))

    def startup_targets(self, devices, layout):
        class BeforeServer(Exception):
            pass

        captured = []
        constructor = adapter.GoveeController

        def capture(*args, **kwargs):
            result = constructor(*args, **kwargs)
            captured.extend(copy.deepcopy(result.targets))
            return result

        previous_config = adapter.lan.CONFIG
        try:
            with TemporaryDirectory(prefix='tmp_composition_startup_') as folder:
                root = Path(folder)
                (root / 'targets.json').write_text(json.dumps({'targets': devices}))
                (root / 'token').write_text('a' * 48)
                with (patch.object(adapter, 'LayoutStore', return_value=layout),
                      patch.object(adapter, 'GRADIENT_MODE', False),
                      patch.object(adapter, 'GoveeController', side_effect=capture),
                      patch.object(adapter, 'ThreadingHTTPServer', side_effect=BeforeServer)):
                    with self.assertRaises(BeforeServer):
                        adapter.main(['--targets', str(root / 'targets.json'),
                                    '--token-file', str(root / 'token'), '--state-dir', str(root)])
        finally:
            adapter.lan.CONFIG = previous_config
        return captured
