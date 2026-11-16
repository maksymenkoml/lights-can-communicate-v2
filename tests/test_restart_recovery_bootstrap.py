"""Exercise real probe startup in a disposable image, without published ports."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

DRIVER = r'''
import ast
import json
import os
import sys
import threading
import time
import traceback
from pathlib import Path

phase, directory, source = sys.argv[1:]
state = Path(directory)
hold = state / "live-output-hold"
if phase == "hold":
    hold.touch()
else:
    hold.unlink(missing_ok=True)
os.environ.update(CONFIG_PATH=str(state), IP="127.0.0.1", MAC="00:17:88:00:fa:01",
                  DEBUG="false", PROBE_HUE_STRIP="true", PROBE_HUE_PLAY="true",
                  PROBE_ENTERTAINMENT="true", PROBE_GOVEE_GRADIENT="true",
                  PROBE_LIVE_OUTPUT="false")
sys.path.insert(0, str(Path(source).parent))
sys.argv = [source, "--docker"]

def wait_for(predicate):
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.02)
    raise AssertionError("Fixture condition was not reached")

def fixture_request(g):
    receiver = g["entertainment_receiver"]
    running = receiver.snapshot().get("udp_guard", {}).get("running")
    assert running, "UDP guard not active in bootstrap"
    intent = g.get("scene_play_intent")
    assert intent is not None, "Play intent controller missing in bootstrap"
    lights = g["config"]["lights"]
    selected = set(g["selected_slots"])
    assert selected == {"1", "2", "3", "4", "5", "6"}
    client = g["app"].test_client()
    headers = {"hue-application-key": g["api_key"]}
    if phase == "seed":
        for lamp in lights.values():
            lamp.state["on"] = False
        room = client.get("/clip/v2/resource/room", headers=headers).get_json()["data"][0]
        xy = {"x": .2, "y": .3}
        body = {"metadata": {"name": "Recovery fixture"},
                "group": {"rid": room["id"], "rtype": "room"},
                "speed": .5,
                "actions": [{"target": {"rid": lights[slot].id_v2, "rtype": "light"},
                             "action": {"on": {"on": False}, "color": {"xy": xy}}}
                            for slot in sorted(selected)],
                "palette": {"color": [{"color": {"xy": xy}},
                                      {"color": {"xy": {"x": .4, "y": .3}}}]}}
        response = client.post("/clip/v2/resource/scene", json=body, headers=headers)
        result = response.get_json()
        assert response.status_code == 200 and not result.get("errors"), result
        scene_id = result["data"][0]["rid"]
        (state / "fixture-scene-id").write_text(scene_id)
        g["configManager"].bridgeConfig.save_config(backup=False)
        response = client.put("/clip/v2/resource/scene/" + scene_id,
                              json={"recall": {"action": "dynamic_palette"}}, headers=headers)
        assert response.status_code == 200 and not response.get_json().get("errors")
        wait_for(lambda: intent.snapshot()["remembered_lights"] == 6)
        response = client.put("/clip/v2/resource/light/" + lights["2"].id_v2,
                              json={"color": {"xy": {"x": .6, "y": .3}}}, headers=headers)
        assert response.status_code == 200
        assert intent.snapshot()["remembered_lights"] == 5
    elif phase == "hold":
        assert receiver.snapshot()["enabled"] is False
        assert intent.snapshot()["remembered_lights"] == 5
        assert intent.snapshot()["replayed_lights"] == 0
    elif phase == "replay":
        wait_for(lambda: intent.snapshot()["replayed_lights"] == 5)
        expected = selected - {"2"}
        wait_for(lambda: all(lights[x].dynamics["status"] == "dynamic_palette" for x in expected))
        assert lights["2"].dynamics["status"] == "none"
        assert all(lamp.state["on"] is False for lamp in lights.values()), "Replay changed power"
        blocked = [thread for thread in threading.enumerate()
                   if not thread.daemon and thread is not threading.main_thread()]
        assert not blocked, "Cold replay created non-daemon workers"
        before = (state / "scene-play-intent.json").read_bytes()
        # Exercise the actual bound group's Sync cache transitions.
        g["area"].update_attr({"stream": {"active": True, "owner": g["api_key"]}})
        g["area"].update_attr({"stream": {"active": False, "owner": None}})
        assert (state / "scene-play-intent.json").read_bytes() == before
        scene_id = (state / "fixture-scene-id").read_text()
        response = client.put("/clip/v2/resource/scene/" + scene_id,
                              json={"recall": {"action": "active"}}, headers=headers)
        assert response.status_code == 200
        assert intent.snapshot()["remembered_lights"] == 0
    else:
        assert phase == "empty"
        assert intent.snapshot()["remembered_lights"] == 0
        assert intent.snapshot()["replayed_lights"] == 0
    public = client.get("/probe/status").get_json()
    assert public["scene_play"] == intent.snapshot()
    assert public["entertainment"]["udp_guard"]["running"]
    print(json.dumps({"phase": phase, "intent": intent.snapshot()}), flush=True)

def fixture_main(g):
    # Werkzeug request threads are daemon threads. Match their context for API
    # calls while leaving the real startup replay in the bootstrap main thread.
    failures = []
    def request():
        try:
            fixture_request(g)
        except BaseException as error:
            failures.append(error)
    thread = threading.Thread(target=request, daemon=True)
    thread.start()
    thread.join(timeout=10)
    assert not thread.is_alive(), "Fixture request did not finish"
    if failures:
        raise failures[0]

class ReplaceMain(ast.NodeTransformer):
    count = 0
    def visit_Call(self, node):
        if isinstance(node.func, ast.Name) and node.func.id == "main" and not node.args:
            self.count += 1
            globals_call = ast.Call(func=ast.Name(id="globals", ctx=ast.Load()),
                                    args=[], keywords=[])
            fixture_call = ast.Call(func=ast.Name(id="__fixture_main", ctx=ast.Load()),
                                    args=[globals_call], keywords=[])
            return ast.copy_location(fixture_call, node)
        return self.generic_visit(node)

tree = ast.parse(Path(source).read_text(), filename=source)
replace = ReplaceMain()
tree = ast.fix_missing_locations(replace.visit(tree))
assert replace.count == 1
namespace = {"__name__": "__main__", "__file__": source, "__fixture_main": fixture_main}
try:
    exec(compile(tree, source, "exec"), namespace)
    assert namespace["entertainment_receiver"].snapshot()["udp_guard"]["running"] is False
except BaseException:
    traceback.print_exc()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(1)
os._exit(0)
'''


@unittest.skipUnless(Path("/opt/hue-emulator/HueEmulator3.py").exists(),
                     "Requires the pinned public diyHue image")
class RestartBootstrapTests(unittest.TestCase):
    def test_cold_replay_partial_cancel_hold_and_shutdown_through_real_bootstrap(self):
        source = Path(__file__).resolve().parents[1] / "experiments/hue-bridge/bootstrap.py"
        with tempfile.TemporaryDirectory(prefix="tmp_restart-bootstrap-") as directory:
            folder = Path(directory)
            state = folder / "state"
            state.mkdir()
            driver = folder / "tmp_bootstrap_driver.py"
            driver.write_text(DRIVER)
            for phase in ("seed", "hold", "replay", "empty"):
                with self.subTest(phase=phase):
                    process = subprocess.run(
                        [sys.executable, "-B", str(driver), phase, str(state), str(source)],
                        capture_output=True, text=True, timeout=30,
                        cwd=state,
                        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                    )
                    self.assertEqual(process.returncode, 0, process.stderr[-2500:])
                    lines = [line for line in process.stdout.splitlines()
                             if line.startswith('{"phase"')]
                    self.assertTrue(lines)
                    self.assertEqual(json.loads(lines[-1])["phase"], phase)


if __name__ == "__main__":
    unittest.main()
