"""Run with the pinned diyHue image, source-only mounts and --network none."""

import ast
import copy
import json
import logging
import sys
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace

sys.path[:0] = ["/opt/hue-emulator", "/probe"]
logging.disable(logging.CRITICAL)
from hue_events import install_event_journal, install_event_route  # noqa: E402

JOURNAL = install_event_journal()

import HueObjects  # noqa: E402
from bridge_output import BridgeOutput, PlayGuard  # noqa: E402
from flask import Flask  # noqa: E402
from gradient_profile import configure_signe, validate_gradient  # noqa: E402
from hue_resource_compat import install_resource_compat  # noqa: E402
from HueObjects import EntertainmentConfiguration, Group, Light  # noqa: E402
from power_feedback import PowerFeedback  # noqa: E402

ORIGINALS = {name: getattr(Light.Light, name) for name in
             ("_setV1State", "genStreamEvent", "getV2Api")}
ORIGINAL_GROUP_API = Group.Group.getV2GroupedLight
ORIGINAL_SLEEP = Light.sleep
ORIGINAL_PUBLICATION = Light.StreamEvent
FUNCTIONS = {"forward_selected_state", "resume_power_color", "publish_power_groups",
             "advertise_selected_power", "power_aware_light_api"}


class UpstreamFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.events, self.sent = [], []
        def capture(message):
            self.events.append(message)
            return ORIGINAL_PUBLICATION(message)

        Light.StreamEvent = capture
        self.guard = PlayGuard()
        self.lights = {}
        for slot in ("1", "2", "3"):
            lamp = Light.Light({"id_v1": slot, "id_v2": "light-" + slot, "name": slot,
                                "modelid": "LCT015", "protocol": "dummy"})
            if slot != "3":
                configure_signe(lamp, True)
                lamp.protocol_cfg["proxy_color_kind"] = "gradient"
            lamp.state["on"] = True
            self.lights[slot] = lamp
        groups = {slot: Group.Group({"id_v1": slot, "name": "test-" + slot,
                                     "type": "Room" if slot == "1" else "LightGroup"})
                  for slot in ("0", "1")}
        groups["90"] = EntertainmentConfiguration.EntertainmentConfiguration({
            "id_v1": "90", "id_v2": "test-entertainment", "name": "Test Sync",
            "configuration_type": "monitor",
        })
        self.groups = groups
        for group in groups.values():
            for lamp in self.lights.values():
                group.add_light(lamp)
        api = SimpleNamespace(v2BridgeEntertainment=lambda: {}, v2BridgeHome=lambda: {
            "id": "bridge-home", "services": []}, geoLocation=lambda: {})
        install_resource_compat(api, Group.Group, "bridge", groups)
        self.roles = {
            "govee-left": {"requested_on": True, "revision": 1, "grant_remaining": 1.5},
            "govee-right": {"requested_on": False, "revision": 1, "grant_remaining": 1.5},
        }
        self.output = BridgeOutput("http://unused", "test", sender=self.sent.append)
        self.ns = {
            "gradient_slots": {"1", "2"}, "validate_gradient": validate_gradient,
            "play_guard": self.guard, "original_set_state": ORIGINALS["_setV1State"],
            "original_light_event": ORIGINALS["genStreamEvent"],
            "original_light_api": ORIGINALS["getV2Api"],
            "selected_slots": {"1", "2"}, "live_output": self.output,
            "config": {"lights": self.lights, "groups": groups, "scenes": {}},
            "StreamEvent": capture, "time": time, "uuid": uuid, "event_journal": JOURNAL,
        }
        tree = ast.parse(Path("/probe/bootstrap.py").read_text())
        functions = [node for node in tree.body
                     if isinstance(node, ast.FunctionDef) and node.name in FUNCTIONS]
        exec(compile(ast.Module(body=functions, type_ignores=[]), "bootstrap.py", "exec"), self.ns)
        self.feedback = PowerFeedback(
            "http://unused", "test", self.lights, self.guard.lock,
            self.ns["resume_power_color"], self.ns["publish_power_groups"],
            fetcher=lambda: {"power_owner": {"enforced": True,
                                             "lights": copy.deepcopy(self.roles)}},
        )
        self.ns["power_feedback"] = self.feedback
        Light.Light._setV1State = self.ns["forward_selected_state"]
        Light.Light.genStreamEvent = self.ns["advertise_selected_power"]
        Light.Light.getV2Api = self.ns["power_aware_light_api"]
        self.events.clear()

    def tearDown(self):
        for name, value in ORIGINALS.items():
            setattr(Light.Light, name, value)
        Group.Group.getV2GroupedLight = ORIGINAL_GROUP_API
        Light.sleep = ORIGINAL_SLEEP
        self.groups["90"].lights = []

    def test_light_and_group_events_match_api_without_broadcasting_wrong_member_state(self):
        self.assertTrue(self.feedback.refresh_once())
        right = self.lights["2"]
        self.assertFalse(right.getV2Api()["on"]["on"])
        light_events = [d for e in self.events for d in e["data"] if d["type"] == "light"]
        self.assertEqual([(e["id"], e["on"]["on"]) for e in light_events], [("light-2", False)])
        groups = [d for e in self.events for d in e["data"] if d["type"] == "grouped_light"]
        self.assertEqual(len(groups), 2)
        self.assertTrue(all(g["on"]["on"] for g in groups))
        self.assertEqual({g["owner"]["rtype"] for g in groups}, {"room", "bridge_home"})
        right.state["on"] = True  # Upstream static scene prewrites before setV1State.
        right.setV2State({"on": {"on": True}})
        self.assertFalse(right.getV2Api()["on"]["on"])
        recent = [d for e in self.events for d in e["data"]
                  if d["type"] == "light" and "on" in d]
        self.assertTrue(all(d["on"]["on"] is False for d in recent))

    def test_real_dynamic_gradient_worker_continues_across_home_off_on(self):
        self.feedback.refresh_once()
        right = self.lights["2"]
        right.dynamics["speed"] = 1
        palette = {"color": [{"color": {"xy": {"x": x, "y": y}}}
                             for x, y in ((0.3, 0.3), (0.5, 0.3), (0.2, 0.5))]}
        turns = []

        def advance(_):
            turns.append(right.getV2Api()["on"]["on"])
            if len(turns) == 1:
                self.roles["govee-right"].update(requested_on=True, revision=2)
                self.assertTrue(self.feedback.refresh_once())
            else:
                right.dynamics["status"] = "none"

        Light.sleep = advance
        self.guard.run(right, palette, 0, Light.Light.dynamicScenePlay)
        self.assertEqual(turns, [False, True])
        self.assertEqual(len(right.state["gradient"]["points"]), 3)
        self.assertTrue(self.output.pending["govee-right"]["state"]["on"])
        self.assertTrue(self.lights["3"].state["on"])

    def test_static_gradient_is_restored_on_home_on_without_cancelling_play_guard(self):
        right = self.lights["2"]
        before = copy.deepcopy(right.state["gradient"])
        generation = self.guard.generations.get("2")
        self.feedback.refresh_once()
        self.roles["govee-right"].update(requested_on=True, revision=2)
        self.feedback.refresh_once()
        value = self.output.pending["govee-right"]
        self.assertEqual(value["transitiontime"], 0)
        self.assertTrue(value["state"]["on"])
        self.assertEqual(value["state"]["gradient"], before)
        self.assertEqual(value["state"]["_color_kind"], "gradient")
        self.assertEqual(self.guard.generations.get("2"), generation)

    def test_actual_light_alias_delivers_to_both_readers_after_legacy_queue_clear(self):
        first, second = JOURNAL.stream(), JOURNAL.stream()
        for stream in (first, second):
            self.assertEqual(next(stream), ": hi\n\n")
        self.feedback.refresh_once()
        HueObjects.eventstream.clear()
        for stream in (first, second):
            frame = next(stream)
            payload = json.loads(next(x[6:] for x in frame.splitlines() if x.startswith("data: ")))
            self.assertEqual(payload[0]["data"][0]["id"], "light-2")
            self.assertFalse(payload[0]["data"][0]["on"]["on"])

    def test_flask_stream_route_emits_live_events_and_replays_after_a_resume_cursor(self):
        app = Flask(__name__)
        app.add_url_rule("/eventstream/clip/v2", "stream.streamV2Events", lambda: "legacy")
        install_event_route(app, JOURNAL,
                            authorize=lambda headers: headers.get("hue-application-key") == "test")
        with app.test_client() as client:
            self.assertEqual(client.get("/eventstream/clip/v2").status_code, 403)
            response = client.get("/eventstream/clip/v2",
                                  headers={"hue-application-key": "test"}, buffered=False)
            try:
                self.assertEqual(response.mimetype, "text/event-stream")
                frames = iter(response.response)
                # Like a Hue bridge: no state snapshot, only the events from now (#70).
                self.assertEqual(next(frames), b": hi\n\n")
                self.feedback.refresh_once()
                event = next(frames).decode()
                self.assertIn('"on":{"on":false}', event)
                epoch, sequence = event.splitlines()[0][4:].split(":")
                cursor = epoch + ":" + str(int(sequence) - 1)
            finally:
                response.close()
            response = client.get("/eventstream/clip/v2",
                                  headers={"Last-Event-ID": cursor, "hue-application-key": "test"},
                                  buffered=False)
            try:
                frames = iter(response.response)
                next(frames)
                self.assertIn('"on":{"on":false}', next(frames).decode())
            finally:
                response.close()

    def test_a_new_reader_gets_no_state_snapshot_only_later_area_updates(self):
        stream = JOURNAL.stream()
        self.assertEqual(next(stream), ": hi\n\n")
        ORIGINAL_PUBLICATION({"type": "update", "data": [{
            "id": "test-entertainment", "type": "entertainment_configuration",
            "metadata": {"name": "new geometry"},
        }]})
        frame = next(stream)
        self.assertIn('"name":"new geometry"', frame)
        self.assertNotIn('"type":"light"', frame)

if __name__ == "__main__":
    unittest.main()
