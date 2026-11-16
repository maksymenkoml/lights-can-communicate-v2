"""Exercise bootstrap capture/rejoin functions without vendor startup or device IO."""
import ast
import copy
import sys
import threading
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'experiments/hue-bridge'))


def bootstrap_functions(*names):
    source = ROOT / 'experiments/hue-bridge/bootstrap.py'
    tree = ast.parse(source.read_text())
    selected = [node for node in ast.walk(tree)
                if isinstance(node, ast.FunctionDef) and node.name in names]
    return compile(ast.Module(body=selected, type_ignores=[]), str(source), 'exec')


class CaptureCompositionTests(unittest.TestCase):
    def test_disabled_color_mode_changes_rejoin_current_mode_without_retained_gradient(self):
        selected = []
        membership = [False]
        lamp = SimpleNamespace(id_v1='1', protocol_cfg={'proxy_color_kind':'gradient'},
                               state={'on':True,'bri':150,'colormode':'xy','xy':[.2,.3],
                                      'ct':200,'gradient':{'points':[]}})

        def set_state(lamp, values, advertise):
            lamp.state.update(values)
            lamp.state['colormode'] = 'ct' if 'ct' in values else 'xy'

        guard = SimpleNamespace(lock=threading.RLock(), worker_generation=lambda _:None,
                                cancel=lambda _:None)
        def submit(slot, state, duration):
            selected.append(copy.deepcopy(state))

        output = SimpleNamespace(slots={'1':'left'}, submit=submit)
        ns = {'GRADIENT_MODE':True,'validate_gradient':lambda _:None,'play_guard':guard,
              'power_feedback':None,'original_set_state':set_state,'selected_slots':{'1'},
              'live_output':output,'layout_store':SimpleNamespace(enabled=lambda _:membership[0])}
        exec(bootstrap_functions('forward_selected_state','resume_power_color'), ns)
        ns['forward_selected_state'](lamp, {'ct':250})
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]['_color_kind'], 'ct')
        membership[0] = True
        ns['resume_power_color'](lamp, full=True)
        self.assertEqual(selected[-1]['_color_kind'], 'ct')
        self.assertEqual(set(selected[-1]['_changed']), {'on','bri','ct'})
        self.assertEqual(selected[-1]['ct'], 250)

    def test_capture_channel_coordinates_match_the_semantic_perimeter_palette(self):
        from entertainment_stream import CAPTURE_POSITIONS

        expected = {2:(0,0),3:(0,.5),4:(0,1),5:(.25,1),6:(.5,1),7:(.75,1),
                    8:(1,1),9:(1,.5),10:(1,0),11:(.25,0),12:(.5,0),13:(.75,0)}
        for channel,(u,v) in expected.items():
            x,y,z = CAPTURE_POSITIONS[channel]
            self.assertAlmostEqual(x, (u-.5)*.8)
            self.assertEqual(y, .8)
            self.assertAlmostEqual(z, (v-.5)*.8)

    def test_capture_api_does_not_read_physical_layout_for_fixed_source_anchors(self):
        from entertainment_stream import CAPTURE_POSITIONS, SLOT_CHANNELS

        lights = {slot:SimpleNamespace(id_v2=str(uuid.uuid5(uuid.NAMESPACE_DNS, slot)))
                  for slot in SLOT_CHANNELS}
        ns = {'original_area_api':lambda _:{}, 'GROUP_ID':'area', 'uuid':uuid,
              'CAPTURE_POSITIONS':CAPTURE_POSITIONS,'SLOT_CHANNELS':SLOT_CHANNELS,
              'config':{'lights':lights},
              'layout_store':SimpleNamespace(snapshot=lambda:(_ for _ in ()).throw(
                  AssertionError('Capture read physical geometry')))}
        exec(bootstrap_functions('desk_area_api'), ns)
        data = ns['desk_area_api'](SimpleNamespace(id_v2='area'))
        self.assertEqual([x['channel_id'] for x in data['channels']],
                         list(range(len(CAPTURE_POSITIONS))))
        self.assertEqual([tuple(x['position'][k] for k in ('x','y','z'))
                          for x in data['channels']], CAPTURE_POSITIONS)
