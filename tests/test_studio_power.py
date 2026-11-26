"""Light Studio switches a lamp through the adapter's owner power paths (fakes only)."""

import copy
import importlib.util
import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

spec = importlib.util.spec_from_file_location(
    "setup_studio_power", Path(__file__).resolve().parents[1] / "studio/server.py")
studio = importlib.util.module_from_spec(spec)
spec.loader.exec_module(studio)


def status(authority="homekit", grant=3600):
    """An adapter status with one Govee lamp and one Hue lamp, both off and online."""
    return {
        "targets": [{"light": "govee-left", "status_age": 0.1,
                     "native": {"identity_verified": True, "onOff": 0, "brightness": 50}}],
        "hue": {"play-left": {"healthy": True, "native_age": 0.5, "native_on": False}},
        "power_owner": {"enforced": True, "authority": authority,
                        "lights": {"govee-left": {"requested_on": False,
                                                  "grant_remaining": grant}}},
        "lights": [
            {"id": "govee-left", "provider": "govee", "model": None, "zones": 7,
             "migrated": True},
            {"id": "play-left", "provider": "hue", "model": None, "zones": 1,
             "migrated": True},
            {"id": "device-0123456789abcdef0123456789abcdef", "provider": "hue",
             "model": "LCX005", "zones": 1, "migrated": False},
        ],
    }


class PowerPathTests(unittest.TestCase):
    def test_govee_takes_the_hue_app_path_and_hue_the_bridge_path(self):
        self.assertEqual(studio.power_paths(status()),
                         {"govee-left": "/power-request", "play-left": "/native-power"})

    def test_a_remote_owner_needs_a_live_grant(self):
        self.assertEqual(studio.power_paths(status("owner", 0)),
                         {"play-left": "/native-power"})
        self.assertIn("govee-left", studio.power_paths(status("owner", 12)))

    def test_without_a_power_authority_a_govee_lamp_has_no_path(self):
        data = status()
        del data["power_owner"]
        self.assertNotIn("govee-left", studio.power_paths(data))

    def test_live_marks_the_lamps_studio_can_switch(self):
        lamps = studio.project_status(status())["lamps"]
        self.assertIs(lamps["govee-left"]["power"], True)
        self.assertIs(lamps["play-left"]["power"], True)
        # Not reported by the bridge: offline, so it cannot be switched.
        self.assertIs(lamps["device-0123456789abcdef0123456789abcdef"]["power"], False)

    def test_a_hue_lamp_in_a_running_sync_is_held_on(self):
        # Its Off would stop Sync: the Entertainment stream holds it on.
        data = status()
        data["hue"]["play-left"]["native_on"] = True
        data["video"] = {"active": True, "ready": True, "frame_age": 0.1}
        data["composition"] = {"enabled_roles": ["play-left", "left"]}
        lamps = studio.project_status(data)["lamps"]
        self.assertIs(lamps["play-left"]["power"], False)
        self.assertEqual(lamps["play-left"]["power_reason"], "sync")
        # A Govee lamp in Sync switches as before; Sync keeps its owner Off.
        self.assertIs(lamps["govee-left"]["power"], True)
        self.assertNotIn("power_reason", lamps["govee-left"])
        # Without Sync the Hue lamp switches again.
        del data["video"]
        self.assertIs(studio.project_status(data)["lamps"]["play-left"]["power"], True)

    def test_an_offline_lamp_cannot_be_switched(self):
        data = status()
        data["hue"]["play-left"]["native_age"] = 30
        self.assertIs(studio.project_status(data)["lamps"]["play-left"]["power"], False)


class StudioPowerTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory(prefix="tmp_studio_power_")
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.status = status()
        self.calls = []
        self.answer = (200, {"accepted": True})

        def post(route, body):
            self.calls.append((route, copy.deepcopy(body)))
            if isinstance(self.answer, Exception):
                raise self.answer
            return self.answer

        server = ThreadingHTTPServer(("127.0.0.1", 0), studio.handler_for(
            self.folder / "missing-token", self.folder,
            live_fetch=lambda: self.status, power_post=post))
        worker = threading.Thread(target=server.serve_forever)
        worker.start()
        self.base = "http://127.0.0.1:" + str(server.server_port)

        def close():
            server.shutdown()
            server.server_close()
            worker.join(2)

        self.addCleanup(close)

    def post(self, body, **headers):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        request = urllib.request.Request(
            self.base + "/api/power", data=data, method="POST",
            headers={"Content-Type": "application/json", "Origin": self.base, **headers})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=5) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as response:
            with response:
                return response.code, json.load(response)

    def test_a_govee_lamp_is_switched_as_a_hue_app_owner_request(self):
        self.assertEqual(self.post({"lamp": "govee-left", "on": True}),
                         (200, {"accepted": True}))
        [(route, body)] = self.calls
        self.assertEqual(route, "/power-request")
        self.assertEqual(set(body), {"id", "issued_at", "states"})
        self.assertRegex(body["id"], r"^[0-9a-f]{32}$")
        self.assertEqual(body["states"], {"govee-left": True})

    def test_a_hue_lamp_is_switched_at_the_bridge(self):
        self.assertEqual(self.post({"lamp": "play-left", "on": False}),
                         (200, {"accepted": True}))
        self.assertEqual(self.calls, [("/native-power", {"lights": {"play-left": False}})])

    def test_a_lamp_without_a_path_is_refused_without_a_write(self):
        self.status["hue"]["play-left"]["native_age"] = 30
        self.assertEqual(self.post({"lamp": "play-left", "on": True}),
                         (409, {"accepted": False, "reason": "unavailable"}))
        self.assertEqual(self.post({"lamp": "device-0123456789abcdef0123456789abcdef",
                                    "on": True}),
                         (409, {"accepted": False, "reason": "unavailable"}))
        self.assertEqual(self.calls, [])

    def test_a_hue_lamp_held_by_sync_is_refused_with_the_reason(self):
        self.status["hue"]["play-left"]["native_on"] = True
        self.status["video"] = {"active": True, "ready": True, "frame_age": 0.1}
        self.status["composition"] = {"enabled_roles": ["play-left"]}
        self.assertEqual(self.post({"lamp": "play-left", "on": False}),
                         (409, {"accepted": False, "reason": "sync active"}))
        self.assertEqual(self.calls, [])

    def test_an_unknown_lamp_is_not_found(self):
        self.assertEqual(self.post({"lamp": "device-ffffffffffffffffffffffffffffffff",
                                    "on": True})[0], 404)
        self.assertEqual(self.calls, [])

    def test_malformed_requests_are_rejected(self):
        for body in ({"lamp": "govee-left"}, {"lamp": "govee-left", "on": 1},
                     {"lamp": "../x", "on": True},
                     {"lamp": "govee-left", "on": True, "brightness": 5}, b"{"):
            self.assertEqual(self.post(body)[0], 400, body)
        self.assertEqual(self.calls, [])

    def test_an_unconfirmed_or_failed_write_is_reported(self):
        self.answer = (503, {"accepted": False})
        self.assertEqual(self.post({"lamp": "govee-left", "on": True}),
                         (503, {"accepted": False, "reason": "not confirmed"}))
        self.answer = OSError("down")
        self.assertEqual(self.post({"lamp": "govee-left", "on": True}),
                         (503, {"accepted": False, "reason": "adapter unavailable"}))

    def test_the_live_output_hold_pauses_power(self):
        (self.folder / "live-output-hold").write_text("")
        self.assertEqual(self.post({"lamp": "govee-left", "on": True}),
                         (409, {"accepted": False, "reason": "paused"}))
        self.assertEqual(self.calls, [])

    def test_a_foreign_origin_cannot_switch(self):
        self.assertEqual(self.post({"lamp": "govee-left", "on": True},
                                   Origin="http://example.com")[0], 403)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
