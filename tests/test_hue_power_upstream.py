"""Actual pinned Hue v1/v2 routes with synthetic owner truth and no live output."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test_restart_recovery_bootstrap import DRIVER

POWER_FIXTURE = r'''
def fixture_request(g):
    import copy
    from hue_power_requests import HuePowerRequests
    from power_feedback import PowerFeedback
    lights = g['config']['lights']
    roles = {role: {'requested_on': True, 'revision': 1, 'grant_remaining': 1.5}
             for role in ('left', 'right')}
    feedback = PowerFeedback('http://unused', 'test', lights, g['play_guard'].lock,
        g['resume_power_color'], g['publish_power_groups'], fetcher=lambda: {
            'power_owner': {'enforced': True, 'roles': copy.deepcopy(roles)}})
    g['power_feedback'] = feedback
    feedback.refresh_once()
    calls = []
    unavailable = [False]
    def sender(states):
        calls.append(states)
        if unavailable[0]:
            raise OSError('Synthetic unavailable controller')
        for role, value in states.items():
            roles[role]['requested_on'] = value
            roles[role]['revision'] += 1
    native_calls = []
    g['power_requests'] = HuePowerRequests('http://unused', 'test', lights,
        g['config']['groups'], feedback, sender=sender,
        native_sender=lambda roles: native_calls.append(roles),
        enabled=lambda: not (state / 'live-output-hold').exists())
    client = g['app'].test_client()
    headers = {'hue-application-key': g['api_key']}
    light_path = '/clip/v2/resource/light/' + lights['2'].id_v2
    assert client.put(light_path, json={'on': {'on': False}}).status_code == 403
    assert calls == []
    for value in (False, True):
        response = client.put(light_path, json={'on': {'on': value}}, headers=headers)
        assert response.status_code == 200, response.get_json()
        assert calls[-1] == {'right': value}
        assert lights['2'].getV2Api()['on']['on'] is value
    response = client.put('/api/' + g['api_key'] + '/lights/1/state', json={'on': False})
    assert response.status_code == 200, response.get_json()
    assert calls[-1] == {'left': False}
    group_id = g['config']['groups']['0'].getV2GroupedLight()['id']
    response = client.put('/clip/v2/resource/grouped_light/' + group_id,
                           json={'on': {'on': True}}, headers=headers)
    assert response.status_code == 200, response.get_json()
    assert calls[-1] == {'left': True, 'right': True}
    native_roles = {slot: role for slot, role in HuePowerRequests.NATIVE_SLOTS.items()
                    if slot in lights}
    if native_roles:
        assert native_calls[-1] == {role: True for role in native_roles.values()}, native_calls
    response = client.put('/api/' + g['api_key'] + '/groups/0/action', json={'on': False})
    assert response.status_code == 200, response.get_json()
    assert calls[-1] == {'left': False, 'right': False}

    # A real gallery-style scene recall retains external Off and sends no power.
    room = client.get('/clip/v2/resource/room', headers=headers).get_json()['data'][0]
    scene = client.post('/clip/v2/resource/scene', headers=headers, json={
        'metadata': {'name': 'Power fixture'},
        'group': {'rid': room['id'], 'rtype': 'room'},
        'actions': [{'target': {'rid': lights['2'].id_v2, 'rtype': 'light'},
                     'action': {'on': {'on': True}, 'color': {'xy': {'x': .2, 'y': .3}}}}],
    })
    assert scene.status_code == 200, scene.get_json()
    before = len(calls)
    scene_id = scene.get_json()['data'][0]['rid']
    response = client.put('/clip/v2/resource/scene/' + scene_id,
                           json={'recall': {'action': 'active'}}, headers=headers)
    assert response.status_code == 200, response.get_json()
    assert len(calls) == before
    assert lights['2'].getV2Api()['on']['on'] is False

    # Rejected physical Off must not leave the virtual lamp falsely Off.
    response = client.put(light_path, json={'on': {'on': True}}, headers=headers)
    assert response.status_code == 200, response.get_json()
    unavailable[0] = True
    response = client.put(light_path, json={'on': {'on': False}}, headers=headers)
    assert response.status_code == 503, response.get_json()
    assert response.get_json()['errors']
    assert lights['2'].getV2Api()['on']['on'] is True
    roles['right']['grant_remaining'] = 0
    response = client.put(light_path, json={'on': {'on': False}}, headers=headers)
    assert response.status_code == 503, response.get_json()
    assert lights['2'].getV2Api()['on']['on'] is True
    assert feedback.snapshot()['last_error'] == 'PowerAuthorityUnavailable'
    def lost_feedback():
        raise OSError('Synthetic lost authority')
    feedback.fetcher = lost_feedback
    response = client.put(light_path, json={'on': {'on': False}}, headers=headers)
    assert response.status_code == 503, response.get_json()
    assert lights['2'].getV2Api()['on']['on'] is True
    response = client.put('/api/' + g['api_key'] + '/lights/2/state', json={'on': False})
    assert response.status_code == 503, response.get_json()
    assert response.get_json()[0]['error']['type'] == 901
    assert lights['2'].getV2Api()['on']['on'] is True
    before = len(calls)
    (state / 'live-output-hold').touch()
    response = client.put(light_path, json={'on': {'on': False}}, headers=headers)
    assert response.status_code == 503, response.get_json()
    assert len(calls) == before
    assert lights['2'].getV2Api()['on']['on'] is True
    print('Actual Hue power API parity PASS', flush=True)
'''


@unittest.skipUnless(Path("/opt/hue-emulator/HueEmulator3.py").exists(),
                     "Requires the pinned public diyHue image")
class HuePowerUpstreamTests(unittest.TestCase):
    def test_actual_v1_v2_light_group_scene_auth_failure_and_hold(self):
        start = DRIVER.index("def fixture_request(g):")
        end = DRIVER.index("def signal_main(g):")
        driver = DRIVER[:start] + POWER_FIXTURE + "\n\n" + DRIVER[end:]
        source = Path(__file__).resolve().parents[1] / "bridge/bootstrap.py"
        with tempfile.TemporaryDirectory(prefix="tmp_hue_power_upstream_") as folder:
            path = Path(folder) / "tmp_power_driver.py"
            path.write_text(driver)
            result = subprocess.run([sys.executable, "-B", str(path), "power", folder, str(source)],
                                    cwd=folder, capture_output=True, text=True, timeout=30,
                                    env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
            self.assertEqual(result.returncode, 0, result.stderr[-4000:] + result.stdout[-1000:])
            self.assertIn("Actual Hue power API parity PASS", result.stdout)
