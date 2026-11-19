import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge"))
from calibration_session import (  # noqa: E402
    DARK_HUE_DIMMING,
    LEASE_SECONDS,
    CalibrationRefused,
    CalibrationSession,
)
from govee_calibration import GoveeCalibration  # noqa: E402
from live_adapter import GoveeController, handler_for  # noqa: E402
from test_govee_calibration import FakeStore  # noqa: E402
from test_live_adapter import LEFT, RIGHT, targets  # noqa: E402

REQUEST = {"lamp": "govee-left", "reference": "play-left", "level": "bright",
           "calibration": {"warmth": 83, "tint": -33, "lowLight": 74}}


class FakeClient:
    def __init__(self):
        self.writes = []

    def write(self, body):
        self.writes.append(body)


class FakeProxy:
    def __init__(self, on=True):
        self.on, self.client = on, FakeClient()
        self.output_lock = threading.RLock()
        self.restored = 0

    def snapshot(self):
        return {"native_on": self.on, "native_age": 0.5}

    def restore_scene(self):
        self.restored += 1


class FakeVideo:
    running = False

    def active(self):
        return self.running


class SessionFixture(unittest.TestCase):
    def setUp(self):
        self.now = 10.0
        self.sent = []
        self.store = FakeStore()
        self.calibration = GoveeCalibration(targets(), self.store)

        def transport(device, command, data):
            self.sent.append((device["side"], command, data))
            return True

        self.controller = GoveeController(targets(), sender=transport, clock=lambda: self.now,
                                          wall_clock=lambda: 1000)
        self.proxies = {role: FakeProxy() for role in ("strip", "hue_left", "hue_center",
                                                        "hue_right")}
        self.video = FakeVideo()
        self.session = CalibrationSession(self.controller, self.calibration, self.proxies,
                                          video=self.video, clock=lambda: self.now)
        self.session.sender = self.calibration.wrap(transport)
        self.fenced = self.session.wrap(transport)
        self.lamps_on()

    def lamps_on(self, left=1, right=1):
        self.controller.update_statuses({LEFT: ({"onOff": left}, self.now),
                                         RIGHT: ({"onOff": right}, self.now)})

    def govee(self, side):
        return [(command, data) for s, command, data in self.sent if s == side]


class CalibrationSessionTests(SessionFixture):
    def test_the_lamp_and_the_reference_show_the_test_white_and_the_rest_is_dark(self):
        self.assertEqual(self.session.update(dict(REQUEST)), {"active": True})
        left = self.govee("left")
        self.assertEqual(left[0][0], "razer", "DreamView is left first")
        # The candidate calibration applies to the calibrated lamp, unsaved.
        self.assertEqual(left[1], ("colorwc", {"color": {"r": 255, "g": 222, "b": 104},
                                               "colorTemInKelvin": 0}))
        self.assertEqual(left[2], ("brightness", {"value": 100}))
        right = self.govee("right")
        self.assertEqual(right[1][1]["color"], {"r": 0, "g": 0, "b": 0})
        self.assertEqual(right[2], ("brightness", {"value": 1}))
        self.assertEqual(self.proxies["hue_left"].client.writes[-1]["dimming"], {"brightness": 100})
        self.assertIn("xy", self.proxies["hue_left"].client.writes[-1]["color"])
        for role in ("strip", "hue_center", "hue_right"):
            self.assertEqual(self.proxies[role].client.writes,
                             [{"dimming": {"brightness": DARK_HUE_DIMMING}}])
        self.assertEqual(self.store.calibration("left")["warmth"], 0, "nothing is saved")

    def test_scene_writes_are_held_and_resent_after_the_session(self):
        self.session.update(dict(REQUEST))
        device = self.controller.by_side["left"]
        self.assertFalse(self.fenced(device, "brightness", {"value": 40}))
        gate = self.session.hold_native(lambda: True)
        self.assertFalse(gate())
        self.controller.last_sent["left"] = {"on": True}
        self.assertTrue(self.session.end("saved"))
        self.assertTrue(gate())
        self.assertTrue(self.fenced(device, "brightness", {"value": 40}))
        self.assertNotIn("left", self.controller.last_sent)
        self.assertTrue(all(proxy.restored == 1 for proxy in self.proxies.values()))
        self.assertEqual(self.session.snapshot()["last_end"], "saved")

    def test_a_renewal_without_changes_sends_nothing_and_a_change_resends(self):
        self.session.update(dict(REQUEST))
        count = len(self.sent)
        self.session.update(dict(REQUEST))
        self.assertEqual(len(self.sent), count)
        self.session.update({**REQUEST, "level": "dim"})
        self.assertEqual(self.govee("left")[-1], ("brightness", {"value": 33}))
        self.assertEqual(self.proxies["hue_left"].client.writes[-1]["dimming"], {"brightness": 10})

    def test_the_session_ends_without_renewal_when_sync_starts_or_a_lamp_turns_off(self):
        for change, reason in ((lambda: None, "expired"),
                               (lambda: setattr(self.video, "running", True), "sync active"),
                               (lambda: self.lamps_on(left=0), "lamp off"),
                               (lambda: setattr(self.proxies["hue_left"], "on", False),
                                "reference off")):
            with self.subTest(reason=reason):
                self.video.running = False
                self.proxies["hue_left"].on = True
                self.lamps_on()
                self.session.update(dict(REQUEST))
                change()
                if reason == "expired":
                    self.now += LEASE_SECONDS + 0.1
                self.session.tick()
                self.assertEqual(self.session.snapshot(), {"active": False, "side": None,
                                                           "reference": None,
                                                           "last_end": reason})
                self.now += 0.1
                self.lamps_on()

    def test_refusals_change_nothing(self):
        self.video.running = True
        with self.assertRaises(CalibrationRefused) as refused:
            self.session.update(dict(REQUEST))
        self.assertEqual(refused.exception.reason, "sync active")
        self.video.running = False
        self.proxies["hue_left"].on = False
        with self.assertRaises(CalibrationRefused) as refused:
            self.session.update(dict(REQUEST))
        self.assertEqual(refused.exception.reason, "reference off")
        self.assertEqual(self.sent, [])
        self.proxies["hue_left"].on = True
        self.session.update(dict(REQUEST))
        with self.assertRaises(CalibrationRefused) as refused:
            self.session.update({**REQUEST, "lamp": "govee-right", "reference": "govee-left"})
        self.assertEqual(refused.exception.reason, "busy")
        for bad in ({**REQUEST, "lamp": "play-left"}, {**REQUEST, "reference": "govee-left"},
                    {**REQUEST, "level": "max"}, {**REQUEST, "calibration": {"warmth": 500}},
                    {"lamp": "govee-left"}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.session.update(bad)

    def test_a_lamp_that_is_off_stays_untouched(self):
        self.proxies["strip"].on = False
        self.lamps_on(right=0)
        self.session.update(dict(REQUEST))
        self.assertEqual(self.govee("right"), [])
        self.assertEqual(self.proxies["strip"].client.writes, [])


class CalibrationRouteTests(SessionFixture):
    def serve(self, **kwargs):
        server = ThreadingHTTPServer(("127.0.0.1", 0),
                                     handler_for(self.controller, "adapter-key", **kwargs))
        worker = threading.Thread(target=server.serve_forever)
        worker.start()

        def close():
            server.shutdown()
            server.server_close()
            worker.join(2)

        self.addCleanup(close)
        return server.server_port

    def post(self, port, path, body, token="adapter-key"):
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json", "X-Adapter-Token": token})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as response:
            with response:
                return response.code, json.load(response)

    def test_calibration_routes(self):
        port = self.serve(calibration=self.session)
        self.assertEqual(self.post(port, "/calibration", REQUEST, token="wrong")[0], 403)
        self.assertEqual(self.post(port, "/calibration", REQUEST), (200, {"active": True}))
        self.assertEqual(self.post(port, "/calibration", {**REQUEST, "level": "x"})[0], 400)
        self.assertEqual(self.post(port, "/calibration/end", {}), (200, {"active": False}))
        self.assertEqual(self.session.snapshot()["last_end"], "ended")
        self.video.running = True
        self.assertEqual(self.post(port, "/calibration", REQUEST),
                         (409, {"active": False, "reason": "sync active"}))

    def test_calibration_routes_are_absent_without_a_session(self):
        port = self.serve()
        self.assertEqual(self.post(port, "/calibration", REQUEST)[0], 404)
        self.assertEqual(self.post(port, "/calibration/end", {})[0], 404)


if __name__ == "__main__":
    unittest.main()
