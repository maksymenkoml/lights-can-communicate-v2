"""Real Node client -> authenticated Python gate -> simulated powering firmware."""

import json
import shutil
import subprocess
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'experiments/hue-bridge'))
from live_adapter import PilotController, handler_for  # noqa: E402
from power_owner import PowerOwner  # noqa: E402
from test_live_adapter import LEFT, RIGHT, targets  # noqa: E402


@unittest.skipUnless(shutil.which('node'), 'Node required for cross-runtime loopback check')
class OwnerIntegrationTests(unittest.TestCase):
    def test_home_off_then_on_is_ordered_before_real_renderer_with_stale_on_cache(self):
        devices = targets()
        for device in devices:
            device['segment_count'] = 7
        physical = {LEFT: 1, RIGHT: 1}
        sent = []
        snapshots = []
        gate = PowerOwner(devices)

        def firmware(device, command, _data):
            if command == 'ptReal':
                physical[device['device']] = 1
            sent.append((device['device'], command))
            return True

        controller = PilotController(devices, sender=gate.wrap(firmware),
                                     clock=lambda: 10, wall_clock=lambda: 1000)
        controller.update_statuses({key: ({'onOff': 1, 'brightness': 65}, 10)
                                    for key in (LEFT, RIGHT)})
        parent = handler_for(controller, 'adapter-key', power_owner=gate, power_token='a' * 48)

        class FixtureHandler(parent):
            # Test-only endpoints simulate the existing Homebridge power writer.
            def do_POST(self):
                if self.path == '/test-power':
                    body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                    if not body['on']:
                        assert not gate.allowed('right'), 'Physical Off preceded color pause'
                    physical[RIGHT] = int(body['on'])
                    return self.respond(200, {})
                if self.path == '/test-render':
                    self.rfile.read(int(self.headers['Content-Length']))
                    sequence = len(snapshots) + 1
                    for side in ('left', 'right'):
                        controller.submit({'side': side, 'session': 'fixture', 'sequence': sequence,
                                           'issued_at': 1000, 'transitiontime': 0,
                                           'state': {'on': True,
                                                     'segments': [[sequence, 20, 30]] * 3}})
                    controller.render()
                    snapshots.append(dict(physical))
                    return self.respond(200, {})
                return super().do_POST()

        server = ThreadingHTTPServer(('127.0.0.1', 0), FixtureHandler)
        worker = threading.Thread(target=server.serve_forever)
        worker.start()
        try:
            result = subprocess.run([
                shutil.which('node'), str(ROOT / 'tests/fixtures/power_owner_flow.mjs'),
                f'http://127.0.0.1:{server.server_port}',
            ], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(snapshots, [{LEFT: 1, RIGHT: 0}, {LEFT: 1, RIGHT: 1}])
            self.assertTrue(any(device == LEFT for device, _ in sent))
            self.assertTrue(any(device == RIGHT for device, _ in sent))
        finally:
            server.shutdown()
            server.server_close()
            worker.join(2)
