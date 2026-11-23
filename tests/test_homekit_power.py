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
from homekit_power import (  # noqa: E402
    HomeKitPower,
    HomeKitRequests,
    response_signature,
    signature,
)
from live_adapter import GoveeController, handler_for  # noqa: E402
from power_requests import PowerRequestUnavailable  # noqa: E402
from test_live_adapter import targets  # noqa: E402

TOKEN, OWNER = "a" * 48, "b" * 64


class FakeLamps:
    """Native Govee state that follows power commands, with an optional delay."""

    def __init__(self, on=True):
        self.now = 100.0
        self.native = {"govee-left": on, "govee-right": on}
        self.reachable = {"govee-left": True, "govee-right": True}
        self.sent = []
        self.ignore = set()
        self.undelivered = set()
        self.lights = {item["device"]: item["light"] for item in targets()}

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds

    def send(self, device, command, data):
        side = self.lights[device["device"]]
        self.sent.append((side, command, data))
        if command in self.undelivered:
            return False
        if command == "turn" and side not in self.ignore:
            self.native[side] = data["value"] == 1
        return True

    def observe(self):
        return {side: {"on": self.native[side] if self.reachable[side] else None,
                       "sample": self.now if self.reachable[side] else None}
                for side in self.native}


class HomeKitPowerTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory(prefix="tmp_homekit_power_")
        self.addCleanup(temp.cleanup)
        self.file = Path(temp.name) / "homekit-power.json"
        self.lamps = FakeLamps()
        self.idle = True
        self.power = self.make()

    def make(self):
        return HomeKitPower(targets(), self.file, self.lamps.send, self.lamps.observe,
                            idle=lambda side: self.idle, clock=self.lamps.clock,
                            sleep=self.lamps.sleep)

    def color_sender(self):
        return self.power.wrap(lambda device, command, data: self.lamps.send(
            device, command, data))

    def test_unknown_intent_is_adopted_from_fresh_native_state(self):
        self.assertFalse(self.power.allowed("govee-left"))
        self.assertEqual(self.power.snapshot()["lights"]["govee-left"]["grant_remaining"], 0)
        self.lamps.native["govee-right"] = False
        self.power.tick()
        self.assertTrue(self.power.allowed("govee-left"))
        self.assertFalse(self.power.allowed("govee-right"))
        roles = self.power.snapshot()["lights"]
        self.assertEqual((roles["govee-left"]["requested_on"],
                          roles["govee-right"]["requested_on"]),
                         (True, False))
        self.assertEqual(roles["govee-left"]["grant_remaining"], 2.0)
        self.assertEqual(self.lamps.sent, [])

    def test_off_closes_color_before_the_power_command_and_on_waits_for_native(self):
        self.power.tick()
        send = self.color_sender()
        right = targets()[1]
        self.assertTrue(send(right, "ptReal", {}))
        revision = self.power.revision("govee-right")
        self.assertTrue(self.power.set({"light": "govee-right", "on": False}))
        self.assertFalse(send(right, "ptReal", {}))
        self.assertGreater(self.power.revision("govee-right"), revision)
        self.assertEqual(self.lamps.sent[-1], ("govee-right", "turn", {"value": 0}))
        self.lamps.sent.clear()
        self.lamps.ignore.add("govee-right")
        self.assertFalse(self.power.set({"light": "govee-right", "on": True}))
        # Native stayed Off: the command was resent once and color stays closed.
        self.assertEqual([item[1] for item in self.lamps.sent], ["turn", "turn"])
        self.assertFalse(send(right, "ptReal", {}))
        self.lamps.ignore.clear()
        self.assertTrue(self.power.set({"light": "govee-right", "on": True}))
        self.assertTrue(send(right, "ptReal", {}))

    def test_off_waits_for_an_in_flight_color_send(self):
        self.power.tick()
        started, release, order = threading.Event(), threading.Event(), []

        def slow(device, command, data):
            started.set()
            release.wait(2)
            order.append(command)
            return True

        send = self.power.wrap(slow)
        worker = threading.Thread(target=send, args=(targets()[1], "ptReal", {}))
        worker.start()
        started.wait(2)
        off = threading.Thread(target=self.power.set, args=({"light": "govee-right", "on": False},))
        off.start()
        release.set()
        worker.join(2)
        off.join(2)
        self.assertEqual(order, ["ptReal"])
        self.assertEqual(self.lamps.sent[0], ("govee-right", "turn", {"value": 0}))

    def test_intent_survives_restart_and_is_private(self):
        self.power.tick()
        self.power.set({"light": "govee-left", "on": False, "hue": 120, "saturation": 50})
        self.assertEqual(self.file.stat().st_mode & 0o777, 0o600)
        restarted = self.make()
        self.lamps.native["govee-left"] = True
        self.assertFalse(restarted.allowed("govee-left"))
        self.assertFalse(restarted.snapshot()["lights"]["govee-left"]["requested_on"])
        state = restarted.state()["lights"]["govee-left"]
        self.assertEqual((state["on"], state["hue"], state["saturation"]), (False, 120, 50))

    def test_external_changes_are_adopted_only_after_settling(self):
        self.power.tick()
        self.power.set({"light": "govee-left", "on": False})
        self.lamps.native["govee-left"] = True  # Govee app turns it back On.
        self.power.tick()
        self.assertFalse(self.power.snapshot()["lights"]["govee-left"]["requested_on"])
        self.lamps.now += 3.1
        self.power.tick()
        self.lamps.now += 1.0
        self.power.tick()
        self.assertFalse(self.power.allowed("govee-left"))
        self.lamps.now += 1.1
        self.power.tick()
        self.assertTrue(self.power.allowed("govee-left"))
        self.lamps.reachable["govee-right"] = False
        self.lamps.native["govee-right"] = False
        self.lamps.now += 10
        self.power.tick()
        self.assertTrue(self.power.snapshot()["lights"]["govee-right"]["requested_on"])

    def test_home_color_and_brightness_apply_only_while_idle(self):
        self.power.tick()
        self.power.set({"light": "govee-left", "brightness": 40})
        self.power.set({"light": "govee-left", "hue": 0, "saturation": 100})
        self.assertEqual(self.lamps.sent[-2:], [
            ("govee-left", "brightness", {"value": 40}),
            ("govee-left", "colorwc",
             {"color": {"r": 255, "g": 0, "b": 0}, "colorTemInKelvin": 0})])
        self.lamps.sent.clear()
        self.idle = False
        self.power.set({"light": "govee-left", "color_temperature": 250})
        self.assertEqual(self.lamps.sent, [])
        self.assertEqual(self.power.state()["lights"]["govee-left"]["color_temperature"], 250)
        self.idle = True
        self.power.set({"light": "govee-left", "on": False})
        self.lamps.sent.clear()
        self.power.set({"light": "govee-left", "on": True})
        self.assertEqual(self.lamps.sent, [
            ("govee-left", "turn", {"value": 1}), ("govee-left", "brightness", {"value": 40}),
            ("govee-left", "colorwc", {"color": {"r": 255, "g": 255, "b": 255},
                                 "colorTemInKelvin": 4000})])

    def test_explicit_home_brightness_or_color_ends_a_hue_scene(self):
        released = []
        scene = {"govee-left": True}
        sync = {"active": False}

        def release(side):
            released.append(side)
            if not sync["active"]:
                scene[side] = False

        self.power = HomeKitPower(targets(), self.file, self.lamps.send, self.lamps.observe,
                                  idle=lambda side: not scene.get(side) and not sync["active"],
                                  clock=self.lamps.clock, sleep=self.lamps.sleep,
                                  release=release)
        self.power.tick()
        # Power alone keeps the scene, so Home On resumes it.
        self.power.set({"light": "govee-left", "on": False})
        self.power.set({"light": "govee-left", "on": True})
        self.assertEqual(released, [])
        self.assertNotIn(("govee-left", "brightness", {"value": 40}), self.lamps.sent)
        self.power.set({"light": "govee-left", "brightness": 40})
        self.assertEqual(released, ["govee-left"])
        self.assertEqual(self.lamps.sent[-1], ("govee-left", "brightness", {"value": 40}))
        # During Sync the release keeps the lamp with Sync: nothing is sent.
        sync["active"] = True
        self.lamps.sent.clear()
        self.power.set({"light": "govee-left", "hue": 0, "saturation": 100})
        self.assertEqual(released, ["govee-left", "govee-left"])
        self.assertEqual(self.lamps.sent, [])
        self.assertEqual(self.power.state()["lights"]["govee-left"]["hue"], 0)

    def test_unconfirmed_power_shows_the_verified_lamp_until_it_confirms(self):
        self.power.tick()
        self.lamps.ignore.add("govee-right")
        self.assertFalse(self.power.set({"light": "govee-right", "on": False}))
        state = self.power.state()["lights"]["govee-right"]
        self.assertEqual((state["on"], state["requested_on"]), (True, False))
        self.assertFalse(self.power.allowed("govee-right"))
        # A late native Off confirms the request without a new command.
        self.lamps.native["govee-right"] = False
        self.power.tick()
        self.assertFalse(self.power.state()["lights"]["govee-right"]["on"])
        self.assertFalse(self.power.unconfirmed["govee-right"])

    def test_undelivered_brightness_or_color_is_not_confirmed(self):
        self.power.tick()
        self.power.set({"light": "govee-left", "brightness": 40})
        self.lamps.undelivered.add("brightness")
        self.assertFalse(self.power.set({"light": "govee-left", "brightness": 30}))
        self.assertEqual(self.power.state()["lights"]["govee-left"]["brightness"], 40)
        self.assertEqual(self.make().state()["lights"]["govee-left"]["brightness"], 40)
        self.lamps.undelivered = {"colorwc"}
        self.assertFalse(self.power.set({"light": "govee-left", "hue": 10, "saturation": 20}))
        self.assertIsNone(self.power.state()["lights"]["govee-left"]["hue"])
        # Turning On applies brightness and color; only the failed color reverts.
        self.power.set({"light": "govee-left", "on": False})
        self.power.set({"light": "govee-left", "brightness": 30})
        self.assertFalse(self.power.set({"light": "govee-left", "on": True, "hue": 200,
                                         "saturation": 50}))
        state = self.power.state()["lights"]["govee-left"]
        self.assertEqual((state["brightness"], state["hue"]), (30, None))
        self.lamps.undelivered.clear()
        self.assertTrue(self.power.set({"light": "govee-left", "hue": 10, "saturation": 20}))

    def test_zero_brightness_is_an_off(self):
        self.power.tick()
        self.assertTrue(self.power.set({"light": "govee-left", "brightness": 0}))
        self.assertFalse(self.power.allowed("govee-left"))
        self.assertEqual(self.lamps.sent, [("govee-left", "turn", {"value": 0})])

    def test_invalid_intents_are_rejected_without_commands(self):
        for payload in ({"light": "strip", "on": True}, {"light": "govee-left"},
                        {"light": "govee-left", "on": 1},
                        {"light": "govee-left", "brightness": 101},
                        {"light": "govee-left", "hue": 400},
                        {"light": "govee-left", "color_temperature": 90},
                        {"light": "govee-left", "unexpected": True}, ["govee-left"]):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.power.set(payload)
        self.assertEqual(self.lamps.sent, [])

    def test_hue_requests_execute_locally_and_report_unconfirmed_power(self):
        self.power.tick()
        requests = HomeKitRequests(self.power, wall_clock=lambda: 1000.0)
        self.assertTrue(requests.request({"id": "c" * 32, "issued_at": 1000.0,
                                          "states": {"govee-right": False}}))
        self.assertFalse(self.power.allowed("govee-right"))
        self.lamps.ignore.add("govee-right")
        with self.assertRaises(PowerRequestUnavailable):
            requests.request({"id": "d" * 32, "issued_at": 1000.0, "states": {"govee-right": True}})
        with self.assertRaises(ValueError):
            requests.request({"id": "e" * 32, "issued_at": 990.0, "states": {"govee-right": True}})


    def test_hue_requests_for_the_settled_state_send_nothing(self):
        self.power.tick()
        requests = HomeKitRequests(self.power, wall_clock=lambda: 1000.0)
        self.assertTrue(requests.request({"id": "c" * 32, "issued_at": 1000.0,
                                          "states": {"govee-right": True}}))
        sent = len(self.lamps.sent)
        self.assertTrue(self.power.settled("govee-right", True))
        self.assertTrue(requests.request({"id": "d" * 32, "issued_at": 1000.0,
                                          "states": {"govee-right": True}}))
        self.assertEqual(len(self.lamps.sent), sent)
        # A lamp switched off elsewhere is not settled, so the request switches it on.
        self.lamps.native["govee-right"] = False
        self.assertFalse(self.power.settled("govee-right", True))
        self.assertTrue(requests.request({"id": "e" * 32, "issued_at": 1000.0,
                                          "states": {"govee-right": True}}))
        self.assertGreater(len(self.lamps.sent), sent)
        self.lamps.reachable["govee-right"] = False
        self.assertFalse(self.power.settled("govee-right", True))
    def test_gate_changes_are_recorded_with_their_cause(self):
        self.power.tick()
        self.assertTrue(self.power.set({"light": "govee-right", "on": False}))
        self.lamps.ignore.add("govee-right")
        self.assertFalse(self.power.set({"light": "govee-right", "on": True}))
        self.lamps.ignore.clear()
        self.lamps.native["govee-right"] = True
        self.power.tick()
        self.assertTrue(self.power.sync_hold("govee-left", send=False))
        self.power.owner_power("govee-left")
        events = [(e["role"], e["cause"], e["requested_on"], e["color_allowed"], e["sync_hold"])
                  for e in self.power.snapshot()["events"]]
        self.assertEqual(events, [
            ("govee-left", "adopted", True, True, False),
            ("govee-right", "adopted", True, True, False),
            ("govee-right", "intent", False, False, False),
            ("govee-right", "intent", True, False, False),
            # A late native reading confirms the On that was not seen in time.
            ("govee-right", "confirmed", True, True, False),
            ("govee-left", "sync_hold", True, False, True),
            ("govee-left", "owner_power", True, True, False),
        ])
        self.assertTrue(all(e["timestamp"].endswith("Z")
                            for e in self.power.snapshot()["events"]))


class HomeKitRouteTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory(prefix="tmp_homekit_route_")
        self.addCleanup(temp.cleanup)
        self.lamps = FakeLamps()
        self.owner_names = {}
        self.power = HomeKitPower(targets(), Path(temp.name) / "intent.json", self.lamps.send,
                                  self.lamps.observe, clock=self.lamps.clock,
                                  sleep=self.lamps.sleep, names=lambda: dict(self.owner_names))
        self.power.tick()
        controller = GoveeController(targets(), sender=self.power.wrap(self.lamps.send),
                                     power_owner=self.power)
        handler = handler_for(controller, TOKEN, power_owner=self.power, power_token=OWNER,
                              power_requests=HomeKitRequests(self.power), homekit=self.power)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.addCleanup(self.server.server_close)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def signed(self, method, path, data, nonce=None, issued=None, key=OWNER):
        timestamp = f"{time.time() if issued is None else issued:.3f}"
        nonce = nonce or uuid.uuid4().hex
        return {"X-LCC-Timestamp": timestamp, "X-LCC-Nonce": nonce,
                "X-LCC-Signature": signature(key, method, path, timestamp, nonce, data or b"")}

    def call(self, path, body=None, token=OWNER, header="X-Power-Owner-Token", headers=None,
             data=None):
        if data is None and body is not None:
            data = json.dumps(body).encode()
        if headers is None:
            headers = (self.signed("POST" if data is not None else "GET", path, data)
                       if path == "/homekit" and token == OWNER else {header: token})
        request = urllib.request.Request(self.url + path, data=data, headers=headers)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=5) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            with error:
                return error.code, json.load(error)

    # /homekit keeps its version-one wire format: the two lamps are named left and right.
    def test_routes_require_the_owner_credential_and_fence_the_legacy_owner(self):
        self.assertEqual(self.call("/homekit", token=TOKEN)[0], 403)
        self.assertEqual(self.call("/homekit", token=TOKEN, header="X-Adapter-Token")[0], 403)
        status, body = self.call("/homekit")
        self.assertEqual(status, 200)
        self.assertEqual(body["roles"]["left"]["on"], True)
        self.assertEqual(self.call("/power-owner", {"session": "x"})[0], 404)
        status, body = self.call("/homekit", {"role": "right", "on": False})
        self.assertEqual((status, body["accepted"], body["roles"]["right"]["on"]),
                         (200, True, False))
        self.assertEqual(self.call("/homekit", {"role": "right", "on": "no"})[0], 400)
        status, body = self.call("/status", token=TOKEN, header="X-Adapter-Token")
        self.assertEqual(body["power_owner"]["authority"], "homekit")
        self.assertFalse(body["power_owner"]["lights"]["govee-right"]["requested_on"])
        self.assertNotIn(OWNER, json.dumps(body))

    def test_signed_requests_cannot_be_replayed_altered_or_replaced_by_the_token(self):
        body = json.dumps({"role": "left", "on": False}).encode()
        headers = self.signed("POST", "/homekit", body)
        self.assertEqual(self.call("/homekit", data=body, headers=headers)[0], 200)
        # The same capture cannot be sent again.
        self.assertEqual(self.call("/homekit", data=body, headers=headers)[0], 403)
        altered = json.dumps({"role": "left", "on": True}).encode()
        self.assertEqual(self.call("/homekit", data=altered,
                                   headers=self.signed("POST", "/homekit", body))[0], 403)
        stale = self.signed("POST", "/homekit", altered, issued=time.time() - 3)
        self.assertEqual(self.call("/homekit", data=altered, headers=stale)[0], 403)
        wrong = self.signed("GET", "/homekit", b"", key="c" * 64)
        self.assertEqual(self.call("/homekit", headers=wrong)[0], 403)
        self.assertEqual(self.call("/homekit", headers={"X-Power-Owner-Token": OWNER})[0], 403)
        self.assertEqual(self.call("/homekit", data=altered,
                                   headers={"X-Power-Owner-Token": OWNER})[0], 403)
        self.assertFalse(self.power.intent["govee-left"]["on"])

    def test_responses_are_signed_for_the_request_nonce(self):
        body = json.dumps({"role": "left", "on": False}).encode()
        headers = self.signed("POST", "/homekit", body)
        request = urllib.request.Request(self.url + "/homekit", data=body, headers=headers)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=5) as response:
            data = response.read()
            self.assertEqual(response.headers["X-LCC-Response"], response_signature(
                OWNER, headers["X-LCC-Nonce"], 200, data))
        try:
            opener.open(urllib.request.Request(self.url + "/homekit", headers=headers),
                        timeout=5)
        except urllib.error.HTTPError as error:
            with error:
                self.assertEqual(error.code, 403)
                self.assertIsNone(error.headers["X-LCC-Response"])
        status = opener.open(urllib.request.Request(
            self.url + "/status", headers={"X-Adapter-Token": TOKEN}), timeout=5)
        with status:
            self.assertIsNone(status.headers["X-LCC-Response"])

    def test_owner_names_are_reported_per_lamp_and_the_response_stays_signed(self):
        status, body = self.call("/homekit")
        self.assertTrue(all("name" not in lamp for lamp in body["roles"].values()))
        # Owner names are keyed by light id (Hue lamps by role); /homekit shows only the two sides.
        self.owner_names.update({"govee-left": "Desk left", "strip": "Not a HomeKit lamp"})
        headers = self.signed("GET", "/homekit", b"")
        request = urllib.request.Request(self.url + "/homekit", headers=headers)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=5) as response:
            data = response.read()
            self.assertEqual(response.headers["X-LCC-Response"], response_signature(
                OWNER, headers["X-LCC-Nonce"], 200, data))
        roles = json.loads(data)["roles"]
        self.assertEqual(roles["left"]["name"], "Desk left")
        self.assertNotIn("name", roles["right"])
        self.assertEqual(set(roles), {"left", "right"})
        status, body = self.call("/homekit", {"role": "right", "on": False})
        self.assertEqual(body["roles"]["left"]["name"], "Desk left")

    def test_signature_matches_the_plugin_vector(self):
        # Same vector as tests/homebridge_plugin.test.mjs.
        self.assertEqual(
            signature("b" * 64, "POST", "/homekit", "1000.000", "c" * 32,
                      b'{"role":"left","on":true}'),
            "7f700addbd1e66dda66bc369cb670882c6e92ce71c92c587658fbf9e2b21fa64")

    def test_unconfirmed_power_returns_service_unavailable(self):
        self.lamps.ignore.add("govee-left")
        status, body = self.call("/homekit", {"role": "left", "on": False})
        self.assertEqual((status, body["accepted"]), (503, False))


if __name__ == "__main__":
    unittest.main()
