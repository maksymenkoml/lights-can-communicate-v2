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
import signal
import sys
import threading
import types
import time
import traceback
from pathlib import Path

phase, directory, source = sys.argv[1:]
state = Path(directory)
hold = state / "live-output-hold"
if phase in {"hold", "signal-hold"}:
    hold.touch()
else:
    hold.unlink(missing_ok=True)
os.environ.update(CONFIG_PATH=str(state), IP="127.0.0.1", MAC="00:17:88:00:fa:01",
                  DEBUG="false", PROBE_HUE_STRIP="true", PROBE_HUE_PLAY="true",
                  PROBE_ENTERTAINMENT="true", PROBE_GOVEE_GRADIENT="true",
                  PROBE_LIVE_OUTPUT="false", PROBE_ADAPTER_URL="http://127.0.0.1:19880")
sys.path.insert(0, str(Path(source).parent))
sys.argv = [source, "--docker"]

# Fail closed even if a future bootstrap edit enables the output constructor.
import bridge_output
class NoLiveOutput:
    def __init__(self, *args, **kwargs):
        raise AssertionError("Live output forbidden in bootstrap fixture")
bridge_output.BridgeOutput = NoLiveOutput

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
        # Exercise the real asynchronous worker boundary deterministically: API
        # acceptance/intent persistence do not promise that playback has started.
        original_factory = g["Scene"].Thread
        def delayed_scene_thread(*args, **kwargs):
            thread = original_factory(*args, **kwargs)
            actual = getattr(thread, "thread", thread)
            original_run = actual.run
            def delayed_run():
                time.sleep(.15)
                original_run()
            actual.run = delayed_run
            return thread
        g["Scene"].Thread = delayed_scene_thread
        response = client.put("/clip/v2/resource/scene/" + scene_id,
                              json={"recall": {"action": "dynamic_palette"}}, headers=headers)
        assert response.status_code == 200 and not response.get_json().get("errors")
        wait_for(lambda: intent.snapshot()["remembered_lights"] == 6)
        wait_for(lambda: all(lights[slot].dynamics["status"] == "dynamic_palette"
                             for slot in selected))
        before = (state / "scene-play-intent.json").read_bytes()
        generation = g["play_guard"].generations["1"]
        response = client.put("/clip/v2/resource/light/" + lights["1"].id_v2,
                              json={"gradient": {"points": []}}, headers=headers)
        assert response.status_code == 400
        assert intent.snapshot()["remembered_lights"] == 6, "Rejected request cancelled intent"
        assert (state / "scene-play-intent.json").read_bytes() == before
        assert g["play_guard"].generations["1"] == generation
        assert lights["1"].dynamics["status"] == "dynamic_palette"
        assert all(lamp.state["on"] is False for lamp in lights.values())
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

def signal_main(g):
    # Execute the actual pinned main and real process signals/hard exit. Only
    # unrelated services are stubbed; no live output constructor is permitted.
    receiver = g["entertainment_receiver"]
    guard = receiver.udp_guard
    events = []
    for lamp in g["config"]["lights"].values():
        lamp.state["on"] = False
    assert g["live_output"] is None
    import entertainment_receiver
    def send_stop(self, message):
        events.append(message["action"])
    entertainment_receiver.StreamOutbox.post = send_stop
    close = guard.close
    def close_guard():
        assert not receiver.serving
        events.append("guard.close")
        return close()
    guard.close = close_guard
    shutdown = receiver.shutdown
    def shutdown_receiver():
        events.append("shutdown.start")
        try:
            return shutdown(timeout=.01 if phase == "signal-timeout" else 5)
        finally:
            # An accepted Start whose worker was delayed must not resurrect
            # the listener after shutdown fenced the generation.
            if receiver.closed:
                old_outbox = receiver.current_outbox
                receiver.serve(g["area"], g["config"]["apiUsers"][g["api_key"]])
                assert receiver.current_outbox is old_outbox
                assert not g["area"].stream["active"]
                events.append("pending.fenced")
            events.append("shutdown.done")
    receiver.shutdown = shutdown_receiver
    if phase == "signal-timeout":
        receiver.serving = True  # A deliberately blocked worker retains the guard.
    elif phase != "signal-hold":
        group = g["area"]
        user = g["config"]["apiUsers"][g["api_key"]]
        user.client_key = "01" * 16
        group.update_attr({"stream": {"active": True, "owner": user.username}})
        worker = threading.Thread(target=receiver.serve, args=(group, user), daemon=True)
        worker.start()
        wait_for(lambda: receiver.snapshot()["active"])
    before_generation = receiver.generation
    # Confirm the vendor still processes unrelated active streams and both saves.
    vendor_group = types.SimpleNamespace(stream={"active": True,
        "_proc": types.SimpleNamespace(kill=lambda: events.append("vendor.kill"))})
    g["config"]["groups"]["fixture-vendor"] = vendor_group
    def save_config(**kwargs):
        events.append("save.backup" if kwargs["backup"] else "save.primary")
        result = {"events": events, "closed": receiver.closed, "serving": receiver.serving,
                  "guard_running": guard.snapshot()["running"],
                  "generation_fenced": receiver.generation is not before_generation,
                  "all_off": all(lamp.state["on"] is False
                                 for lamp in g["config"]["lights"].values())}
        (state / "signal-result.json").write_text(json.dumps(result))
    g["configManager"].bridgeConfig.save_config = save_config
    class NoServiceThread:
        def __init__(self, *args, **kwargs):
            pass
        def start(self):
            pass
    import services
    for name in ("mqtt", "deconz", "ssdp", "mdns", "scheduler", "remoteApi",
                 "remoteDiscover", "entertainment", "stateFetch", "eventStreamer",
                 "homeAssistantWS", "updateManager"):
        module = types.ModuleType("services." + name)
        module.__getattr__ = lambda name: (lambda *args, **kwargs: None)
        sys.modules[module.__name__] = module
        setattr(services, name, module)
    def run_http(*args):
        assert signal.signal is original_signal
        assert HueEmulator3.signal is signal
        os.kill(os.getpid(), signal.SIGINT if phase == "signal-int" else signal.SIGTERM)
        raise AssertionError("Vendor signal handler returned instead of hard exit")
    original_signal = signal.signal
    import HueEmulator3
    assert HueEmulator3.signal is signal
    globals_copy = dict(g["main"].__globals__, Thread=NoServiceThread, runHttp=run_http)
    actual_main = types.FunctionType(g["main"].__code__, globals_copy,
                                    closure=g["main"].__closure__)
    assert signal.signal is original_signal
    actual_main()

def fixture_main(g):
    if phase == "error":
        raise RuntimeError("Synthetic main failure")
    if phase.startswith("signal-"):
        return signal_main(g)
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
    if phase == "error":
        receiver = namespace["entertainment_receiver"]
        print(json.dumps({"guard_closed": not receiver.udp_guard.snapshot()["running"],
                          "receiver_closed": receiver.closed}), flush=True)
    traceback.print_exc()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(1)
os._exit(0)
'''


@unittest.skipUnless(Path("/opt/hue-emulator/HueEmulator3.py").exists(),
                     "Requires the pinned public diyHue image")
class RestartBootstrapTests(unittest.TestCase):
    def test_bootstrap_main_error_still_shuts_down_receiver(self):
        source = Path(__file__).resolve().parents[1] / "bridge/bootstrap.py"
        with tempfile.TemporaryDirectory(prefix="tmp_restart-error-") as directory:
            folder = Path(directory)
            driver = folder / "tmp_error_driver.py"
            driver.write_text(DRIVER)
            process = subprocess.run(
                [sys.executable, "-B", str(driver), "error", str(folder), str(source)],
                capture_output=True, text=True, timeout=30, cwd=folder,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
            self.assertEqual(process.returncode, 1, process.stderr[-2500:])
            self.assertIn("Synthetic main failure", process.stderr)
            self.assertIn('{"guard_closed": true, "receiver_closed": true}', process.stdout)

    def test_actual_upstream_signals_shutdown_before_vendor_hard_exit(self):
        source = Path(__file__).resolve().parents[1] / "bridge/bootstrap.py"
        for phase in ("signal-term", "signal-int", "signal-hold", "signal-timeout"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory(
                    prefix="tmp_restart-signal-") as directory:
                folder = Path(directory)
                driver = folder / "tmp_signal_driver.py"
                driver.write_text(DRIVER)
                process = subprocess.run(
                    [sys.executable, "-B", str(driver), phase, str(folder), str(source)],
                    capture_output=True, text=True, timeout=30, cwd=folder,
                    env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
                self.assertEqual(process.returncode, 0, process.stderr[-2500:])
                result = json.loads((folder / "signal-result.json").read_text())
                self.assertTrue(result["closed"], result)
                self.assertTrue(result["generation_fenced"], result)
                self.assertTrue(result["all_off"], result)
                events = result["events"]
                self.assertEqual(events[-3:], ["vendor.kill", "save.primary", "save.backup"])
                self.assertLess(events.index("pending.fenced"), events.index("shutdown.done"))
                self.assertLess(events.index("shutdown.done"), events.index("vendor.kill"))
                if phase == "signal-timeout":
                    self.assertTrue(result["guard_running"], result)
                    self.assertNotIn("guard.close", events)
                else:
                    self.assertFalse(result["guard_running"], result)
                    self.assertFalse(result["serving"], result)
                    if phase != "signal-hold":
                        self.assertLess(events.index("stop"), events.index("guard.close"))
                    self.assertLess(events.index("guard.close"), events.index("shutdown.done"))

    def test_cold_replay_partial_cancel_hold_and_shutdown_through_real_bootstrap(self):
        source = Path(__file__).resolve().parents[1] / "bridge/bootstrap.py"
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
