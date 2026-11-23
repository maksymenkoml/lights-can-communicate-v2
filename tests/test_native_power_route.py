"""The adapter switches only pinned native Hue lights, on authenticated requests."""

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge"))
from live_adapter import GoveeController, handler_for  # noqa: E402
from test_live_adapter import targets  # noqa: E402


class Proxy:
    def __init__(self, confirm=True):
        self.calls, self.confirm = [], confirm

    def set_power(self, on):
        self.calls.append(on)
        if self.confirm == "error":
            raise OSError("bridge unreachable")
        return self.confirm

    def snapshot(self):
        return {"native_on": None}


class NativePowerRouteTests(unittest.TestCase):
    def setUp(self):
        self.strip, self.left = Proxy(), Proxy()
        handler = handler_for(GoveeController(targets()), "adapter-key",
                              hue_proxies={"hue-monitor": self.strip, "play-left": self.left})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.worker = threading.Thread(target=self.server.serve_forever)
        self.worker.start()
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(2)

    def post(self, body, token="adapter-key"):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.server.server_port}/native-power",
            data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json", "X-Adapter-Token": token})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as response:
            with response:
                return response.code, json.load(response)

    def test_confirmed_lights_are_switched(self):
        self.assertEqual(self.post({"lights": {"hue-monitor": True, "play-left": False}}),
                         (200, {"accepted": True}))
        self.assertEqual((self.strip.calls, self.left.calls), ([True], [False]))

    def test_unconfirmed_or_failed_power_is_reported(self):
        self.left.confirm = False
        self.assertEqual(self.post({"lights": {"play-left": True}}), (503, {"accepted": False}))
        self.left.confirm = "error"
        self.assertEqual(self.post({"lights": {"play-left": True}}), (503, {"accepted": False}))

    def test_unknown_lights_invalid_values_and_bad_tokens_switch_nothing(self):
        for body in ({"lights": {"play-right": True}}, {"lights": {"hue-monitor": "on"}},
                     {"lights": {}}, {"states": {"hue-monitor": True}},
                     # Old role names and the old body shape are not accepted.
                     {"lights": {"strip": True}}, {"roles": {"hue-monitor": True}},
                     {"lights": {"hue-monitor": True}, "roles": {"hue-monitor": True}}):
            with self.subTest(body=body):
                self.assertEqual(self.post(body)[0], 400)
        self.assertEqual(self.post({"lights": {"hue-monitor": True}}, token="wrong")[0], 403)
        self.assertEqual((self.strip.calls, self.left.calls), ([], []))


if __name__ == "__main__":
    unittest.main()
