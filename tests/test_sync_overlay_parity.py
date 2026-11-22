"""Keep the displayed functional boundary aligned with the actual Sync model."""
import copy
import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bridge'))
from desk_layout import validate_layout  # noqa: E402
from sync_zone import validate_zone, vertical_bounds, zone_points  # noqa: E402
from test_sync_projection import layout, sync_profile  # noqa: E402


class SyncOverlayParityTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'UI parity requires the local Node runtime')
    def test_custom_zone_validation_and_sections_match_client(self):
        root = Path(__file__).resolve().parents[1]
        base = {'version':1,'points':[{'u':u,'h':h} for u,h in
                                     ((-2,0),(2,0),(2,-65),(1,-15),(-1,-15),(-2,-65))]}
        skew = copy.deepcopy(base)
        skew['points'][0]['h'] = 10
        narrow = copy.deepcopy(base)
        for point in narrow['points'][2:]:
            point['h'] = -1+5e-9
        invalid = copy.deepcopy(base)
        invalid['points'][3]['u'] = -3
        near_vertex = copy.deepcopy(base)
        for point in near_vertex['points'][2:]:
            point['h'] = -15
        near_vertex['points'][3]['u'] = -0.9999999999999999
        cases = [base,skew,narrow,invalid,near_vertex]
        xs = [-100,-60,0,20,50,100,150,180,260,300]
        script = """
const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const source=fs.readFileSync('tests/setup_studio_client.test.cjs','utf8').split('\\ntest(')[0];
const cases=JSON.parse(process.argv[1]),xs=JSON.parse(process.argv[2]);
vm.runInNewContext(source+`
harness().then(app=>console.log(JSON.stringify(cases.map(config=>{
 const valid=app.run('validSyncZone('+JSON.stringify(config)+')');
 if(!valid)return {valid};
 // The client works in desk-centred centimetres; the layout's world x is centre + 100.
 app.run('environment.zones.sync='+JSON.stringify(config.points));
 const bounds=xs.map(x=>JSON.parse(app.run('JSON.stringify(syncBounds('+(x-100)+'))')));
 const world='zonePoints("sync").map(p=>({x:p.x+100,height:p.h}))';
 return {valid,points:JSON.parse(app.run('JSON.stringify('+world+')')),bounds};
}))));`,{require,__dirname:path.join(process.cwd(),'tests'),structuredClone,
       AbortController,setImmediate,console,cases,xs});
"""
        result = subprocess.run(['node','-e',script,json.dumps(cases),json.dumps(xs)],
                                cwd=root,capture_output=True,text=True,timeout=10,check=True)
        actual = json.loads(result.stdout)
        for config, found in zip(cases,actual):
            try:
                validate_zone(config)
                valid = True
            except ValueError:
                valid = False
            self.assertEqual(found['valid'],valid)
            if not valid:
                continue
            data = layout()
            data['monitorInches'] = 32
            data['syncZone'] = config
            points = zone_points(validate_layout(data))
            self.assertEqual(found['points'],points)
            for x,bounds in zip(xs,found['bounds']):
                expected = vertical_bounds(points,x)
                if expected is None:
                    self.assertIsNone(bounds)
                else:
                    for a,b in zip(bounds,expected):
                        self.assertAlmostEqual(a,b)

    @unittest.skipUnless(shutil.which('node'), 'UI parity requires the local Node runtime')
    def test_frontend_boundary_matches_backend_for_desk_sizes_and_lateral_distances(self):
        root = Path(__file__).resolve().parents[1]
        cases = [[width,height,x] for width in (80,160,240) for height in (50,85,120)
                 for x in (-100,5,20,100,180,195,300)]
        script = """
const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const source=fs.readFileSync('tests/setup_studio_client.test.cjs','utf8').split('\\ntest(')[0];
const cases=JSON.parse(process.argv[1]);
vm.runInNewContext(source+`
harness().then(app=>{
 const result=cases.map(([w,h,x])=>{
  // Govee Right at world x; the floor lamp's fade is the zone height at its x.
  app.run('environment.width='+w+';environment.height='+h+';lamps[1].x='+(x-100));
  return app.run('(b=>b?b[1]-b[0]:0)(syncBounds(lamps[1].x))');
 });
 console.log(JSON.stringify(result));
});`,{require,__dirname:path.join(process.cwd(),'tests'),structuredClone,
      AbortController,setImmediate,console,cases});
"""
        result = subprocess.run(['node','-e',script,json.dumps(cases)], cwd=root,
                                capture_output=True, text=True, timeout=10, check=True)
        actual = json.loads(result.stdout)
        self.assertEqual(len(actual), len(cases))
        for case, client_fade in zip(cases, actual):
            width,height,x = case
            data = copy.deepcopy(layout())
            data['desk'] = {'width':width,'height':height}
            data['devices'][1]['positionCm']['x'] = x
            fade = sync_profile(data)['govee']['right']['fade_cm']
            with self.subTest(width=width,height=height,x=x):
                self.assertAlmostEqual(client_fade, fade)
