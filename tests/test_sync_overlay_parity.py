"""Keep the displayed functional boundary aligned with the actual Sync model."""
import copy
import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'experiments/hue-bridge'))
from sync_projection import sync_profile  # noqa: E402
from test_sync_projection import layout  # noqa: E402


class SyncOverlayParityTests(unittest.TestCase):
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
  app.run('layout.desk.width='+w+';layout.desk.height='+h);
  return app.run('syncFadeY('+(x*5)+')');
 });
 console.log(JSON.stringify(result));
});`,{require,__dirname:path.join(process.cwd(),'tests'),structuredClone,
      AbortController,setImmediate,console,cases});
"""
        result = subprocess.run(['node','-e',script,json.dumps(cases)], cwd=root,
                                capture_output=True, text=True, timeout=10, check=True)
        actual = json.loads(result.stdout)
        self.assertEqual(len(actual), len(cases))
        for case, y in zip(cases, actual):
            width,height,x = case
            data = copy.deepcopy(layout())
            data['desk'] = {'width':width,'height':height}
            data['devices'][1]['positionCm']['x'] = x
            fade = sync_profile(data)['govee']['right']['fade_cm']
            with self.subTest(width=width,height=height,x=x):
                self.assertAlmostEqual(y, 850-(height-fade)*5)
