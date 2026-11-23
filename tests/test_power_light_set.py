"""Power, HomeKit and Sync-off state keyed by Govee light id (#7, stage 3a)."""

import json
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge"))
import legacy_wire  # noqa: E402
import live_adapter as adapter  # noqa: E402
from bridge_output import BridgeOutput  # noqa: E402
from homekit_power import HomeKitPower, HomeKitRequests, signature  # noqa: E402
from power_feedback import PowerFeedback  # noqa: E402
from power_owner import PowerOwner  # noqa: E402
from power_requests import PowerRequests, PowerRequestUnavailable  # noqa: E402
from sync_off import STATE_FILE, SyncOffSession  # noqa: E402
from test_govee_light_set import FOURTH, FOURTH_ID, THIRD, THIRD_ID  # noqa: E402
from test_live_adapter import LEFT, RIGHT, targets  # noqa: E402

TOKEN, OWNER = "a" * 48, "b" * 64


def three():
    return targets() + [{"light": THIRD_ID, "device": THIRD, "ip": "192.168.10.52",
                         "brightness_scale": 1}]


class Lamps:
    """Native power of any number of lights that follows ``turn`` commands."""

    def __init__(self, lights, on=True):
        self.now = 100.0
        self.native = {light: on for light in lights}
        self.of = {item["device"]: item["light"] for item in three() + [
            {"light": FOURTH_ID, "device": FOURTH}]}
        self.sent = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds

    def send(self, device, command_name, data):
        light = self.of[device["device"]]
        self.sent.append((light, command_name, data))
        if command_name == "turn":
            self.native[light] = data["value"] == 1
        return True

    def observe(self):
        return {light: {"on": on, "sample": self.now} for light, on in self.native.items()}


class HomeKitLightSetTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory(prefix="tmp_power_light_set_")
        self.addCleanup(temp.cleanup)
        self.file = Path(temp.name) / "homekit-power.json"
        self.lamps = Lamps(("govee-left", "govee-right", THIRD_ID))

    def make(self, selected=None):
        return HomeKitPower(selected or three(), self.file, self.lamps.send, self.lamps.observe,
                            clock=self.lamps.clock, sleep=self.lamps.sleep)

    def test_every_light_has_its_own_intent_gate_and_revision(self):
        power = self.make()
        power.tick()
        self.assertEqual(power.lights, ("govee-left", "govee-right", THIRD_ID))
        self.assertTrue(all(power.allowed(light) for light in power.lights))
        revision = power.revision("govee-left")
        self.assertTrue(power.set({"light": THIRD_ID, "on": False}))
        self.assertFalse(power.allowed(THIRD_ID))
        self.assertTrue(power.allowed("govee-left"))
        self.assertEqual(power.revision("govee-left"), revision)
        self.assertEqual(self.lamps.sent, [(THIRD_ID, "turn", {"value": 0})])
        self.assertEqual(set(power.snapshot()["lights"]), set(power.lights))
        self.assertEqual(set(power.state()["lights"]), set(power.lights))

    def test_a_light_that_is_not_driven_is_refused(self):
        power = self.make()
        for light in ("left", FOURTH_ID, "strip", None):
            with self.subTest(light=light), self.assertRaises(ValueError):
                power.set({"light": light, "on": True})
        with self.assertRaises(ValueError):
            power.sync_hold(FOURTH_ID)
        self.assertEqual(self.lamps.sent, [])
        # The old payload key is gone.
        with self.assertRaises(ValueError):
            power.set({"role": "govee-left", "on": True})

    def test_hue_app_requests_name_lights_and_reject_unknown_ones(self):
        power = self.make()
        power.tick()
        requests = HomeKitRequests(power, wall_clock=lambda: 1000.0)
        body = {"id": "c" * 32, "issued_at": 1000.0}
        self.assertTrue(requests.request({**body, "states": {THIRD_ID: False}}))
        self.assertEqual(self.lamps.sent, [(THIRD_ID, "turn", {"value": 0})])
        for states in ({"left": True}, {FOURTH_ID: True}, {"govee-left": True, "right": True}):
            with self.subTest(states=states), self.assertRaises(ValueError):
                requests.request({**body, "id": "d" * 32, "states": states})
        self.assertEqual(len(self.lamps.sent), 1)

    def test_version_one_state_converts_once_and_is_rewritten_as_version_two(self):
        old = {"version": 1, "roles": {
            "left": {"on": False, "brightness": 40, "hue": 120, "saturation": 50, "kelvin": None},
            "right": {"on": True, "brightness": None, "hue": None, "saturation": None,
                      "kelvin": 3000}}}
        self.file.write_text(json.dumps(old))
        power = self.make(targets())
        self.assertIs(power.intent["govee-left"]["on"], False)
        self.assertEqual(power.intent["govee-left"]["brightness"], 40)
        self.assertEqual(power.intent["govee-right"]["kelvin"], 3000)
        saved = json.loads(self.file.read_text())
        self.assertEqual(saved["version"], 2)
        self.assertEqual(set(saved), {"version", "lights"})
        self.assertEqual(saved["lights"], json.loads(json.dumps(power.intent)))
        self.assertEqual(self.file.stat().st_mode & 0o777, 0o600)
        # The old keys are not read again: only the version-two keys count now.
        saved["lights"]["govee-left"]["on"] = True
        self.file.write_text(json.dumps({**saved, "roles": old["roles"]}))
        again = self.make(targets())
        self.assertIs(again.intent["govee-left"]["on"], True)

    def test_version_two_state_round_trips_and_ignores_lights_that_are_gone(self):
        power = self.make()
        power.tick()
        self.assertTrue(power.set({"light": THIRD_ID, "on": False, "brightness": 30}))
        saved = json.loads(self.file.read_text())
        self.assertEqual(set(saved["lights"]), set(power.lights))
        restarted = self.make()
        self.assertIs(restarted.intent[THIRD_ID]["on"], False)
        self.assertEqual(restarted.intent[THIRD_ID]["brightness"], 30)
        smaller = self.make(targets())
        self.assertEqual(set(smaller.intent), {"govee-left", "govee-right"})

    def test_an_unreadable_or_future_state_is_unknown_intent(self):
        for content in ("{broken", json.dumps({"version": 3, "lights": {}}), "[]"):
            with self.subTest(content=content[:10]):
                self.file.write_text(content)
                power = self.make()
                self.assertTrue(all(power.intent[light]["on"] is None for light in power.lights))

    def test_a_failed_rewrite_still_loads_the_converted_state(self):
        self.file.write_text(json.dumps({"version": 1, "roles": {
            "left": {"on": True}, "right": {"on": False}}}))
        original = HomeKitPower._save

        def failing(self):
            raise OSError("read-only")

        HomeKitPower._save = failing
        try:
            power = self.make(targets())
        finally:
            HomeKitPower._save = original
        self.assertIs(power.intent["govee-right"]["on"], False)


class HomeKitWireTests(unittest.TestCase):
    """/homekit names every Govee lamp, the third one included, by its device ID."""

    def setUp(self):
        temp = TemporaryDirectory(prefix="tmp_homekit_wire_")
        self.addCleanup(temp.cleanup)
        self.lamps = Lamps(("govee-left", "govee-right", THIRD_ID))
        self.power = HomeKitPower(three(), Path(temp.name) / "intent.json", self.lamps.send,
                                  self.lamps.observe, clock=self.lamps.clock,
                                  sleep=self.lamps.sleep)
        self.power.tick()
        self.sent = []
        controller = adapter.GoveeController(
            three(), sender=lambda *args: self.sent.append(args) or True, power_owner=self.power)
        handler = adapter.handler_for(controller, TOKEN, power_owner=self.power,
                                      power_token=OWNER,
                                      power_requests=HomeKitRequests(self.power),
                                      homekit=self.power)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.addCleanup(self.server.server_close)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def call(self, path, body=None, header="X-Power-Owner-Token", token=OWNER):
        data = None if body is None else json.dumps(body).encode()
        if path == "/homekit":
            timestamp, nonce = f"{time.time():.3f}", uuid.uuid4().hex
            headers = {"X-LCC-Timestamp": timestamp, "X-LCC-Nonce": nonce,
                       "X-LCC-Signature": signature(OWNER, "POST" if data else "GET", path,
                                                    timestamp, nonce, data or b"")}
        else:
            headers = {header: token}
        request = urllib.request.Request(self.url + path, data=data, headers=headers)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=5) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            with error:
                return error.code, json.load(error)

    def test_get_lists_every_govee_lamp_by_device_id(self):
        status, body = self.call("/homekit")
        self.assertEqual(status, 200)
        self.assertEqual(set(body), {"version", "lights"})
        self.assertEqual(set(body["lights"]), {LEFT, RIGHT, THIRD})
        self.assertEqual(body["version"], 2)

    def test_post_names_the_device_and_reaches_a_third_lamp(self):
        status, body = self.call("/homekit", {"device": THIRD, "on": False})
        self.assertEqual((status, body["accepted"], body["lights"][THIRD]["on"]),
                         (200, True, False))
        self.assertFalse(self.power.allowed(THIRD_ID))
        self.assertTrue(self.power.allowed("govee-right"))
        # The device ID is matched without regard to case or separators.
        status, body = self.call("/homekit", {"device": RIGHT.lower().replace(":", ""),
                                              "on": False})
        self.assertEqual((status, body["lights"][RIGHT]["on"]), (200, False))

    def test_the_wire_refuses_roles_light_ids_and_unknown_devices(self):
        for payload in ({"role": "right", "on": False}, {"device": FOURTH, "on": False},
                        {"device": THIRD_ID, "on": False}, {"light": "govee-left", "on": False},
                        {"device": "middle", "on": False}, {"device": LEFT, "role": "left",
                                                            "on": False},
                        {"on": False}):
            with self.subTest(payload=payload):
                self.assertEqual(self.call("/homekit", payload)[0], 400)
        self.assertEqual(self.lamps.sent, [])

    def test_power_request_takes_light_ids_and_rejects_unknown_ones_with_400(self):
        body = {"id": "a" * 32, "issued_at": time.time(), "states": {THIRD_ID: False}}
        status, _ = self.call("/power-request", body, "X-Adapter-Token", TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(self.lamps.sent, [(THIRD_ID, "turn", {"value": 0})])
        for states in ({FOURTH_ID: True}, {"left": True}):
            self.lamps.sent.clear()
            body = {"id": "b" * 32, "issued_at": time.time(), "states": states}
            self.assertEqual(self.call("/power-request", body, "X-Adapter-Token", TOKEN)[0], 400)
            self.assertEqual(self.lamps.sent, [])


class OwnerLightSetTests(unittest.TestCase):
    def test_three_lights_are_gated_separately_and_the_legacy_wire_sees_two(self):
        lamps = Lamps(("govee-left", "govee-right", THIRD_ID))
        gate = PowerOwner(three(), clock=lambda: 10, wall_clock=lambda: 1000)
        controller = adapter.GoveeController(three(), sender=gate.wrap(lamps.send),
                                             clock=lambda: 10, wall_clock=lambda: 1000,
                                             power_owner=gate)
        controller.update_statuses({
            adapter.lan.device_key(item): (
                {"onOff": 1, "identity_verified": True, "native_sample": 10}, 10)
            for item in three()})
        server = ThreadingHTTPServer(("127.0.0.1", 0), adapter.handler_for(
            controller, "adapter-key", power_owner=gate, power_token="o" * 48))
        worker = threading.Thread(target=server.serve_forever)
        worker.start()
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/power-owner",
                data=json.dumps({"session": "s", "sequence": 1, "issued_at": 1000,
                                 "states": {"left": True, "right": False}}).encode(),
                headers={"X-Power-Owner-Token": "o" * 48}, method="POST")
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(request) as response:
                body = json.load(response)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(2)
        self.assertEqual(set(body["observed"]), {"left", "right"})
        self.assertTrue(gate.allowed("govee-left"))
        self.assertFalse(gate.allowed("govee-right"))
        # The overlay never grants the light it does not know.
        self.assertFalse(gate.allowed(THIRD_ID))
        self.assertEqual(set(gate.snapshot()["lights"]), {"govee-left", "govee-right", THIRD_ID})

    def test_the_owner_requests_name_lights_and_reject_unknown_ones(self):
        gate = PowerOwner(three(), clock=lambda: 10, wall_clock=lambda: 1000)
        broker = PowerRequests(gate, observe=lambda: {})
        self.addCleanup(broker.close)
        for states in ({FOURTH_ID: True}, {"left": True}):
            with self.subTest(states=states), self.assertRaises(ValueError):
                broker.request({"id": "a" * 32, "issued_at": 1000, "states": states})
        with self.assertRaises(PowerRequestUnavailable):
            broker.request({"id": "a" * 32, "issued_at": 1000, "states": {THIRD_ID: True}})

    def test_the_owner_refuses_a_light_it_does_not_drive(self):
        gate = PowerOwner(three(), clock=lambda: 10, wall_clock=lambda: 1000)
        for states in ({"left": True}, {FOURTH_ID: True}):
            with self.subTest(states=states), self.assertRaises(ValueError):
                gate.submit({"session": "s", "sequence": 1, "issued_at": 1000, "states": states})
        with self.assertRaises(ValueError):
            PowerOwner(three() + [{**three()[2], "device": FOURTH}])


class LegacyWireTests(unittest.TestCase):
    def test_owner_envelope_translates_states_and_capability_revisions(self):
        wire = {"session": "s", "sequence": 1, "issued_at": 1000,
                "states": {"left": True, "right": False},
                "power_requests": {"version": 1, "revisions": {"left": 2}, "results": []}}
        result = legacy_wire.owner_envelope(wire)
        self.assertEqual(result["states"], {"govee-left": True, "govee-right": False})
        self.assertEqual(result["power_requests"]["revisions"], {"govee-left": 2})
        self.assertEqual(wire["states"], {"left": True, "right": False})
        for bad in ({"states": {"third": True}}, {"states": {"govee-left": True}},
                    {"power_requests": {"revisions": {"govee-left": 1}}}, [], None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                legacy_wire.owner_envelope(bad)

    def test_exchange_and_observations_come_back_as_sides(self):
        result = legacy_wire.owner_exchange({"power_requests": [
            {"id": "a" * 32, "light": "govee-right", "on": True, "issued_at": 1,
             "expires_at": 2, "revision": 3}], "power_request_acks": []})
        self.assertEqual(result["power_requests"], [
            {"id": "a" * 32, "role": "right", "on": True, "issued_at": 1, "expires_at": 2,
             "revision": 3}])
        self.assertEqual(legacy_wire.owner_exchange({}), {})
        self.assertEqual(legacy_wire.owner_observed(
            {"govee-left": {"on": True}, THIRD_ID: {"on": False}}), {"left": {"on": True}})

    def test_legacy_wire_no_longer_translates_homekit(self):
        # /homekit names lamps by Govee device ID (HomeKitPower.wire_state/wire_intent).
        self.assertFalse(hasattr(legacy_wire, "homekit_intent"))
        self.assertFalse(hasattr(legacy_wire, "homekit_state"))


class FakeDiyHueLight:
    def __init__(self):
        self.state = {}

    def genStreamEvent(self, event):
        pass


class FeedbackAndGatewayTests(unittest.TestCase):
    def feedback(self, payload):
        lights = {"1": FakeDiyHueLight(), "2": FakeDiyHueLight()}
        feedback = PowerFeedback("http://unused", "t", lights, threading.RLock(),
                                 resume=lambda lamp: None, publish_groups=lambda slots: None,
                                 fetcher=lambda: payload)
        return feedback, lights

    def authority(self, **lights):
        return {"power_owner": {"enforced": True, "lights": {
            name: {"requested_on": on, "revision": 1, "grant_remaining": 1.5}
            for name, on in lights.items()}}}

    def test_the_virtual_slots_follow_their_govee_lights_among_any_number(self):
        payload = self.authority(**{"govee-left": True, "govee-right": False, THIRD_ID: True})
        feedback, lights = self.feedback(payload)
        self.assertTrue(feedback.refresh_once())
        self.assertEqual(feedback.states, {"1": True, "2": False})
        self.assertIs(lights["2"].state["on"], False)

    def test_a_status_without_the_slot_lights_is_unavailable(self):
        feedback, _ = self.feedback(self.authority(**{THIRD_ID: True}))
        self.assertFalse(feedback.refresh_once())
        self.assertEqual(feedback.states, {})
        old = {"power_owner": {"enforced": True, "roles": {"left": {}, "right": {}}}}
        feedback, _ = self.feedback(old)
        self.assertFalse(feedback.refresh_once())

    def test_slots_one_and_two_still_drive_the_migrated_lights(self):
        sent = []
        output = BridgeOutput("http://adapter", "t", sender=sent.append, clock=lambda: 1000.0)
        self.assertEqual(output.slots, {"1": "govee-left", "2": "govee-right"})
        self.assertTrue(output.submit("1", {"on": True, "bri": 254, "xy": [0.7, 0.299]}, 20))
        self.assertTrue(output.submit("2", {"on": True, "bri": 254, "xy": [0.7, 0.299]}, 20))
        self.assertFalse(output.submit("3", {"on": True}, 20))
        output.forward_once()
        output.forward_once()
        self.assertEqual([item["light"] for item in sent], ["govee-left", "govee-right"])
        controller = adapter.GoveeController(targets(), clock=lambda: 0.0,
                                             wall_clock=lambda: 1000.0)
        for payload in sent:
            self.assertTrue(controller.submit(payload))
        self.assertEqual(set(controller.jobs), {"govee-left", "govee-right"})


class SyncOffLightSetTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory(prefix="tmp_sync_off_light_set_")
        self.addCleanup(temp.cleanup)
        self.state = Path(temp.name) / STATE_FILE
        self.lamps = Lamps(("govee-left", "govee-right", THIRD_ID))
        self.owner = HomeKitPower(three(), Path(temp.name) / "homekit.json", self.lamps.send,
                                  self.lamps.observe, clock=self.lamps.clock,
                                  sleep=self.lamps.sleep)
        self.owner.tick()

    def session(self, **options):
        return SyncOffSession(None, lambda: False, self.state, govee=self.owner,
                              observe=self.lamps.observe, clock=self.lamps.clock, **options)

    def test_legacy_roles_convert_once_and_the_file_is_rewritten(self):
        self.state.write_text(json.dumps({"version": 1, "roles": ["left", "strip", "right"]}))
        session = self.session(native={})
        self.assertEqual(session.snapshot()["roles"], ["govee-left", "govee-right", "strip"])
        self.assertEqual(json.loads(self.state.read_text()),
                         {"version": 1, "roles": ["govee-left", "govee-right", "strip"]})
        self.assertTrue(self.owner.sync_held("govee-left"))
        self.assertTrue(self.owner.sync_held("govee-right"))
        self.assertFalse(self.owner.sync_held(THIRD_ID))

    def test_light_ids_survive_a_restart_and_unknown_ones_are_dropped(self):
        self.state.write_text(json.dumps({"version": 1, "roles": [
            THIRD_ID, FOURTH_ID, "govee-middle", "device-X", 3, "unknown"]}))
        session = self.session(native={})
        self.assertEqual(session.snapshot()["roles"], [THIRD_ID])
        self.assertTrue(self.owner.sync_held(THIRD_ID))
        # A light that is no longer driven is dropped instead of failing the start.
        smaller = HomeKitPower(targets(), self.state.parent / "small.json", self.lamps.send,
                               self.lamps.observe, clock=self.lamps.clock,
                               sleep=self.lamps.sleep)
        self.state.write_text(json.dumps({"version": 1, "roles": [THIRD_ID, "govee-left"]}))
        gone = SyncOffSession(None, lambda: False, self.state, govee=smaller, native={},
                              observe=self.lamps.observe, clock=self.lamps.clock)
        self.assertEqual(gone.snapshot()["roles"], ["govee-left"])

    def test_a_third_light_is_switched_off_and_restored_with_the_others_untouched(self):
        class Layout:
            def sync_off_roles(self):
                return [THIRD_ID]

        session = SyncOffSession(Layout(), lambda: False, self.state, govee=self.owner,
                                 native={}, observe=self.lamps.observe, clock=self.lamps.clock)
        session.start()
        self.assertEqual(self.lamps.sent, [(THIRD_ID, "turn", {"value": 0})])
        self.assertEqual(json.loads(self.state.read_text())["roles"], [THIRD_ID])
        self.assertFalse(self.owner.allowed(THIRD_ID))
        self.assertTrue(self.owner.allowed("govee-left"))
        session.end()
        self.assertEqual(self.lamps.sent[-1], (THIRD_ID, "turn", {"value": 1}))
        self.assertTrue(self.owner.allowed(THIRD_ID))
        self.assertFalse(self.state.exists())


if __name__ == "__main__":
    unittest.main()
