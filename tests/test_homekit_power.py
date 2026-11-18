import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "experiments/hue-bridge"))
from homekit_power import HomeKitPower, HomeKitRequests  # noqa: E402
from live_adapter import PilotController, handler_for  # noqa: E402
from power_requests import PowerRequestUnavailable  # noqa: E402
from test_live_adapter import targets  # noqa: E402

TOKEN, OWNER = "a" * 48, "b" * 64


class FakeLamps:
    """Native Govee state that follows power commands, with an optional delay."""

    def __init__(self, on=True):
        self.now = 100.0
        self.native = {"left": on, "right": on}
        self.reachable = {"left": True, "right": True}
        self.sent = []
        self.ignore = set()
        self.sides = {item["device"]: item["side"] for item in targets()}

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds

    def send(self, device, command, data):
        side = self.sides[device["device"]]
        self.sent.append((side, command, data))
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
        self.assertFalse(self.power.allowed("left"))
        self.assertEqual(self.power.snapshot()["roles"]["left"]["grant_remaining"], 0)
        self.lamps.native["right"] = False
        self.power.tick()
        self.assertTrue(self.power.allowed("left"))
        self.assertFalse(self.power.allowed("right"))
        roles = self.power.snapshot()["roles"]
        self.assertEqual((roles["left"]["requested_on"], roles["right"]["requested_on"]),
                         (True, False))
        self.assertEqual(roles["left"]["grant_remaining"], 2.0)
        self.assertEqual(self.lamps.sent, [])

    def test_off_closes_color_before_the_power_command_and_on_waits_for_native(self):
        self.power.tick()
        send = self.color_sender()
        right = targets()[1]
        self.assertTrue(send(right, "ptReal", {}))
        revision = self.power.revision("right")
        self.assertTrue(self.power.set({"role": "right", "on": False}))
        self.assertFalse(send(right, "ptReal", {}))
        self.assertGreater(self.power.revision("right"), revision)
        self.assertEqual(self.lamps.sent[-1], ("right", "turn", {"value": 0}))
        self.lamps.sent.clear()
        self.lamps.ignore.add("right")
        self.assertFalse(self.power.set({"role": "right", "on": True}))
        # Native stayed Off: the command was resent once and color stays closed.
        self.assertEqual([item[1] for item in self.lamps.sent], ["turn", "turn"])
        self.assertFalse(send(right, "ptReal", {}))
        self.lamps.ignore.clear()
        self.assertTrue(self.power.set({"role": "right", "on": True}))
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
        off = threading.Thread(target=self.power.set, args=({"role": "right", "on": False},))
        off.start()
        release.set()
        worker.join(2)
        off.join(2)
        self.assertEqual(order, ["ptReal"])
        self.assertEqual(self.lamps.sent[0], ("right", "turn", {"value": 0}))

    def test_intent_survives_restart_and_is_private(self):
        self.power.tick()
        self.power.set({"role": "left", "on": False, "hue": 120, "saturation": 50})
        self.assertEqual(self.file.stat().st_mode & 0o777, 0o600)
        restarted = self.make()
        self.lamps.native["left"] = True
        self.assertFalse(restarted.allowed("left"))
        self.assertFalse(restarted.snapshot()["roles"]["left"]["requested_on"])
        state = restarted.state()["roles"]["left"]
        self.assertEqual((state["on"], state["hue"], state["saturation"]), (False, 120, 50))

    def test_external_changes_are_adopted_only_after_settling(self):
        self.power.tick()
        self.power.set({"role": "left", "on": False})
        self.lamps.native["left"] = True  # Govee app turns it back On.
        self.power.tick()
        self.assertFalse(self.power.snapshot()["roles"]["left"]["requested_on"])
        self.lamps.now += 3.1
        self.power.tick()
        self.lamps.now += 1.0
        self.power.tick()
        self.assertFalse(self.power.allowed("left"))
        self.lamps.now += 1.1
        self.power.tick()
        self.assertTrue(self.power.allowed("left"))
        self.lamps.reachable["right"] = False
        self.lamps.native["right"] = False
        self.lamps.now += 10
        self.power.tick()
        self.assertTrue(self.power.snapshot()["roles"]["right"]["requested_on"])

    def test_home_color_and_brightness_apply_only_while_idle(self):
        self.power.tick()
        self.power.set({"role": "left", "brightness": 40})
        self.power.set({"role": "left", "hue": 0, "saturation": 100})
        self.assertEqual(self.lamps.sent[-2:], [
            ("left", "brightness", {"value": 40}),
            ("left", "colorwc", {"color": {"r": 255, "g": 0, "b": 0}, "colorTemInKelvin": 0})])
        self.lamps.sent.clear()
        self.idle = False
        self.power.set({"role": "left", "color_temperature": 250})
        self.assertEqual(self.lamps.sent, [])
        self.assertEqual(self.power.state()["roles"]["left"]["color_temperature"], 250)
        self.idle = True
        self.power.set({"role": "left", "on": False})
        self.lamps.sent.clear()
        self.power.set({"role": "left", "on": True})
        self.assertEqual(self.lamps.sent, [
            ("left", "turn", {"value": 1}), ("left", "brightness", {"value": 40}),
            ("left", "colorwc", {"color": {"r": 255, "g": 255, "b": 255},
                                 "colorTemInKelvin": 4000})])

    def test_zero_brightness_is_an_off(self):
        self.power.tick()
        self.assertTrue(self.power.set({"role": "left", "brightness": 0}))
        self.assertFalse(self.power.allowed("left"))
        self.assertEqual(self.lamps.sent, [("left", "turn", {"value": 0})])

    def test_invalid_intents_are_rejected_without_commands(self):
        for payload in ({"role": "strip", "on": True}, {"role": "left"},
                        {"role": "left", "on": 1}, {"role": "left", "brightness": 101},
                        {"role": "left", "hue": 400}, {"role": "left", "color_temperature": 90},
                        {"role": "left", "unexpected": True}, ["left"]):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.power.set(payload)
        self.assertEqual(self.lamps.sent, [])

    def test_hue_requests_execute_locally_and_report_unconfirmed_power(self):
        self.power.tick()
        requests = HomeKitRequests(self.power, wall_clock=lambda: 1000.0)
        self.assertTrue(requests.request({"id": "c" * 32, "issued_at": 1000.0,
                                          "states": {"right": False}}))
        self.assertFalse(self.power.allowed("right"))
        self.lamps.ignore.add("right")
        with self.assertRaises(PowerRequestUnavailable):
            requests.request({"id": "d" * 32, "issued_at": 1000.0, "states": {"right": True}})
        with self.assertRaises(ValueError):
            requests.request({"id": "e" * 32, "issued_at": 990.0, "states": {"right": True}})


class HomeKitRouteTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory(prefix="tmp_homekit_route_")
        self.addCleanup(temp.cleanup)
        self.lamps = FakeLamps()
        self.power = HomeKitPower(targets(), Path(temp.name) / "intent.json", self.lamps.send,
                                  self.lamps.observe, clock=self.lamps.clock,
                                  sleep=self.lamps.sleep)
        self.power.tick()
        controller = PilotController(targets(), sender=self.power.wrap(self.lamps.send),
                                     power_owner=self.power)
        handler = handler_for(controller, TOKEN, power_owner=self.power, power_token=OWNER,
                              power_requests=HomeKitRequests(self.power), homekit=self.power)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.addCleanup(self.server.server_close)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def call(self, path, body=None, token=OWNER, header="X-Power-Owner-Token"):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.url + path, data=data, headers={header: token})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=5) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            with error:
                return error.code, json.load(error)

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
        self.assertFalse(body["power_owner"]["roles"]["right"]["requested_on"])
        self.assertNotIn(OWNER, json.dumps(body))

    def test_unconfirmed_power_returns_service_unavailable(self):
        self.lamps.ignore.add("left")
        status, body = self.call("/homekit", {"role": "left", "on": False})
        self.assertEqual((status, body["accepted"]), (503, False))


if __name__ == "__main__":
    unittest.main()
