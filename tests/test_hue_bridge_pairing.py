"""Pairing with the physical Hue bridge (#60 stage 1)."""

import importlib.util
import json
import stat
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge"))
from hue_bridge_pairing import (  # noqa: E402
    DEVICE_TYPE,
    BridgeError,
    Configured,
    HueBridgePairing,
    OtherBridge,
)

BRIDGE_ID = "ecb5fafffe0a1b2c"
KEY = "Ab3-" + "k" * 36
CLIENT_KEY = "0123456789ABCDEF0123456789ABCDEF"
APPLICATION = "6d0a2f4e-1b2c-4d3e-8f90-123456789abc"
SECRETS = (KEY, CLIENT_KEY, APPLICATION)


class Bridge:
    """A fake verified bridge: answers by path; the link button is pressed after
    ``presses`` registration attempts."""

    def __init__(self, identity=BRIDGE_ID, answer_id=None, presses=0, success=None):
        self.identity, self.answer_id = identity, answer_id or identity
        self.presses, self.calls = presses, []
        self.success = success or {"username": KEY, "clientkey": CLIENT_KEY}

    def __call__(self, ip, method, path, body=None, key=None, timeout=None):
        self.calls.append((method, path, body, key))
        if path == "/api/0/config":
            return 200, {"name": "Hue Bridge", "bridgeid": self.answer_id.upper()}, \
                self.identity, {}
        if path == "/api":
            if self.presses > 0:
                self.presses -= 1
                return 200, [{"error": {"type": 101, "description": "link button"}}], \
                    self.identity, {}
            return 200, [{"success": self.success}], self.identity, {}
        if path == "/auth/v1" and key == KEY:
            return 200, None, self.identity, {"hue-application-id": APPLICATION}
        return 403, None, self.identity, {}


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class PairingTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory(prefix="tmp_hue_pairing_")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.clock = Clock()

    def pairing(self, bridge, has_hue_lights=lambda: False, find=lambda: ["192.168.10.5"]):
        return HueBridgePairing(self.root / "hue-bridge.json", self.root / "hue-bridge-id",
                                has_hue_lights=has_hue_lights, transport=bridge, find=find,
                                clock=self.clock, sleep=self.clock.sleep)

    def wait(self, pairing):
        for _ in range(500):
            if pairing.status()["state"] != "pairing":
                return pairing.status()
            threading.Event().wait(0.01)
        self.fail("pairing did not end")

    def test_pairing_after_the_link_button_stores_the_keys_privately(self):
        bridge = Bridge(presses=3)
        pairing = self.pairing(bridge)
        self.assertEqual(pairing.find(), {"bridges": [{"ip": "192.168.10.5",
                                                        "name": "Hue Bridge"}]})
        # The fake bridge answers at once, so the attempt may already be done.
        self.assertIn(pairing.pair("192.168.10.5")["state"], ("pairing", "paired"))
        status = self.wait(pairing)
        self.assertEqual(status, {"state": "paired", "name": "Hue Bridge",
                                  "ip": "192.168.10.5", "result": "paired"})
        stored = json.loads((self.root / "hue-bridge.json").read_text())
        self.assertEqual(stored, {"bridge": "192.168.10.5", "bridge_id": BRIDGE_ID,
                                  "name": "Hue Bridge", "hue_key": KEY,
                                  "client_key": CLIENT_KEY, "application_id": APPLICATION})
        for name in ("hue-bridge.json", "hue-bridge-id"):
            self.assertEqual(stat.S_IMODE((self.root / name).stat().st_mode), 0o600)
        self.assertEqual((self.root / "hue-bridge-id").read_text(), BRIDGE_ID + "\n")
        registrations = [call for call in bridge.calls if call[1] == "/api"]
        self.assertEqual(len(registrations), 4)
        self.assertEqual(registrations[0][2], {"devicetype": DEVICE_TYPE,
                                               "generateclientkey": True})
        # Only the application ID request carries the new key.
        self.assertEqual([call[1] for call in bridge.calls if call[3] is not None],
                         ["/auth/v1"])

    def test_no_answer_ever_contains_a_key_or_the_application_id(self):
        pairing = self.pairing(Bridge())
        answers = [pairing.find(), pairing.pair("192.168.10.5"), self.wait(pairing),
                   pairing.status(), pairing.cancel()]
        text = json.dumps(answers)
        for secret in SECRETS:
            self.assertNotIn(secret, text)

    def test_a_button_that_is_never_pressed_times_out_without_a_file(self):
        pairing = self.pairing(Bridge(presses=10_000))
        pairing.pair("192.168.10.5")
        self.assertEqual(self.wait(pairing), {"state": "none", "result": "timeout"})
        self.assertFalse((self.root / "hue-bridge.json").exists())
        self.assertFalse((self.root / "hue-bridge-id").exists())

    def test_cancel_stops_the_attempt(self):
        gate = threading.Event()

        class Slow(Bridge):
            def __call__(self, ip, method, path, body=None, key=None, timeout=None):
                if path == "/api":
                    gate.wait(2)
                return super().__call__(ip, method, path, body=body, key=key)

        pairing = self.pairing(Slow())
        pairing.pair("192.168.10.5")
        self.assertEqual(pairing.cancel(), {"state": "none"})
        gate.set()
        threading.Event().wait(0.1)
        self.assertFalse((self.root / "hue-bridge.json").exists())

    def test_only_a_verified_bridge_naming_its_certificate_id_is_found_or_paired(self):
        imposter = Bridge(answer_id="0000000000000000")
        pairing = self.pairing(imposter)
        self.assertEqual(pairing.find(), {"bridges": []})
        with self.assertRaises(BridgeError):
            pairing.pair("192.168.10.5")

        def unverified(*args, **kwargs):
            raise OSError("certificate verify failed")

        pairing = self.pairing(unverified)
        self.assertEqual(pairing.find(), {"bridges": []})
        with self.assertRaises(BridgeError):
            pairing.pair("192.168.10.5")
        for address in ("8.8.8.8", "127.0.0.1", "not an address"):
            with self.subTest(address=address), self.assertRaises(ValueError):
                self.pairing(Bridge()).pair(address)

    def test_another_bridge_is_refused_while_its_hue_lights_are_connected(self):
        (self.root / "hue-bridge-id").write_text("1111111111111111\n")
        with self.assertRaises(OtherBridge):
            self.pairing(Bridge(), has_hue_lights=lambda: True).pair("192.168.10.5")
        self.assertEqual((self.root / "hue-bridge-id").read_text(), "1111111111111111\n")
        # Without connected Hue lights the new bridge replaces the pin.
        pairing = self.pairing(Bridge())
        pairing.pair("192.168.10.5")
        self.assertEqual(self.wait(pairing)["state"], "paired")
        self.assertEqual((self.root / "hue-bridge-id").read_text(), BRIDGE_ID + "\n")

    def test_invalid_keys_or_a_missing_application_id_fail(self):
        pairing = self.pairing(Bridge(success={"username": "short", "clientkey": CLIENT_KEY}))
        pairing.pair("192.168.10.5")
        self.assertEqual(self.wait(pairing), {"state": "none", "result": "failed"})

        class NoApplication(Bridge):
            def __call__(self, ip, method, path, body=None, key=None, timeout=None):
                if path == "/auth/v1":
                    return 200, None, self.identity, {}
                return super().__call__(ip, method, path, body=body, key=key)

        pairing = self.pairing(NoApplication())
        pairing.pair("192.168.10.5")
        self.assertEqual(self.wait(pairing), {"state": "none", "result": "failed"})
        self.assertFalse((self.root / "hue-bridge.json").exists())

    def test_a_bridge_from_the_service_configuration_is_not_paired_again(self):
        explicit = self.root / "hue.json"
        explicit.write_text('{"bridge": "192.168.10.5", "hue_key": "PRIVATE"}')
        bridge = Bridge()
        pairing = HueBridgePairing(self.root / "hue-bridge.json", self.root / "hue-bridge-id",
                                   transport=bridge, configured=explicit)
        self.assertEqual(pairing.status(), {"state": "configured"})
        with self.assertRaises(Configured):
            pairing.pair("192.168.10.5")
        self.assertEqual(bridge.calls, [])
        # The paired file named as the service's own file is an ordinary pairing.
        pairing = HueBridgePairing(self.root / "hue-bridge.json", self.root / "hue-bridge-id",
                                   transport=bridge, configured=self.root / "hue-bridge.json")
        self.assertEqual(pairing.status(), {"state": "none"})

    def test_a_damaged_file_reads_as_not_paired(self):
        (self.root / "hue-bridge.json").write_text('{"bridge": "192.168.10.5"}')
        self.assertEqual(self.pairing(Bridge()).status(), {"state": "none"})



def studio_module():
    spec = importlib.util.spec_from_file_location(
        "setup_studio", Path(__file__).resolve().parents[1] / "studio/server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class StudioHueBridgeRouteTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory(prefix="tmp_studio_hue_bridge_")
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.clock = Clock()

    def serve(self, bridge, pin=None, registry=None):
        if pin is not None:
            (self.folder / "hue-bridge-id").write_text(pin + "\n")
        if registry is not None:
            (self.folder / "lights.json").write_text(registry)
        studio = studio_module()
        pairing = HueBridgePairing(
            self.folder / "hue-bridge.json", self.folder / "hue-bridge-id",
            transport=bridge, find=lambda: ["192.168.10.5"], clock=self.clock,
            sleep=self.clock.sleep)
        # The route wires its own registry check; reuse it for this pairing.
        handler = studio.handler_for(self.folder / "missing-token",
                                     self.folder / "desk-layout.json", hue_bridge=pairing)
        pairing.has_hue_lights = handler.has_hue_lights
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        worker = threading.Thread(target=server.serve_forever)
        worker.start()
        self.addCleanup(lambda: (pairing.cancel(), server.shutdown(), server.server_close(),
                                 worker.join(2)))
        self.base = "http://127.0.0.1:" + str(server.server_port)
        return pairing

    def call(self, path, body=None):
        data = body if isinstance(body, bytes) or body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            self.base + path, data=data, method="GET" if body is None else "POST",
            headers={"Content-Type": "application/json", "Origin": self.base})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=5) as answer:
                return answer.status, json.load(answer)
        except urllib.error.HTTPError as answer:
            with answer:
                return answer.code, json.load(answer)

    def test_find_pair_and_status_never_show_a_key(self):
        pairing = self.serve(Bridge())
        self.assertEqual(self.call("/api/hue-bridge"), (200, {"state": "none"}))
        self.assertEqual(self.call("/api/hue-bridge/find", {}),
                         (200, {"bridges": [{"ip": "192.168.10.5", "name": "Hue Bridge"}]}))
        code, _ = self.call("/api/hue-bridge/pair", {"ip": "192.168.10.5"})
        self.assertEqual(code, 200)
        for _ in range(500):
            if pairing.status()["state"] != "pairing":
                break
            threading.Event().wait(0.01)
        code, status = self.call("/api/hue-bridge")
        self.assertEqual((code, status["state"]), (200, "paired"))
        for secret in SECRETS:
            self.assertNotIn(secret, json.dumps(status))

    def test_requests_are_validated(self):
        self.serve(Bridge())
        self.assertEqual(self.call("/api/hue-bridge/pair", {"ip": "8.8.8.8"})[0], 400)
        self.assertEqual(self.call("/api/hue-bridge/pair", {"ip": "192.168.10.5", "x": 1})[0],
                         400)
        self.assertEqual(self.call("/api/hue-bridge/find", {"x": 1})[0], 400)
        self.assertEqual(self.call("/api/hue-bridge/forget", {})[0], 405)
        self.assertEqual(self.call("/api/hue-bridge/pair", b"x" * 300)[0], 413)

    def test_another_bridge_with_connected_hue_lights_is_refused(self):
        registry = json.dumps({"lights": [{"id": "hue-left", "provider": "hue"}]})
        self.serve(Bridge(), pin="1111111111111111", registry=registry)
        self.assertEqual(self.call("/api/hue-bridge/pair", {"ip": "192.168.10.5"}),
                         (409, {"error": "another Hue bridge has connected lights",
                                "reason": "other_bridge"}))
        # An unreadable registry cannot prove that no Hue light depends on the pin.
        (self.folder / "lights.json").write_text("not json")
        self.assertEqual(self.call("/api/hue-bridge/pair", {"ip": "192.168.10.5"})[0], 409)

    def test_a_configured_bridge_is_reported_and_not_paired(self):
        studio = studio_module()
        (self.folder / "hue.json").write_text('{"bridge": "192.168.10.5"}')
        handler = studio.handler_for(self.folder / "missing-token",
                                     self.folder / "desk-layout.json",
                                     hue_config=self.folder / "hue.json")
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        worker = threading.Thread(target=server.serve_forever)
        worker.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), worker.join(2)))
        self.base = "http://127.0.0.1:" + str(server.server_port)
        self.assertEqual(self.call("/api/hue-bridge"), (200, {"state": "configured"}))
        self.assertEqual(self.call("/api/hue-bridge/pair", {"ip": "192.168.10.5"}),
                         (409, {"error": "the service configuration names a Hue bridge",
                                "reason": "configured"}))

    def test_studio_searches_the_lan_through_the_adapter(self):
        studio = studio_module()
        (self.folder / "token").write_text("private-adapter-key")
        asked = []

        def answer(url, timeout, headers=None):
            asked.append((url, headers))
            return {"addresses": ["192.168.10.21", 7]}

        studio._read_local_json = answer
        handler = studio.handler_for(self.folder / "token", self.folder / "desk-layout.json",
                                     adapter_url="http://adapter:19880")
        self.assertEqual(handler.lan_search("/lan/hue-bridges", lambda: ["local"])(),
                         ["192.168.10.21"])
        self.assertEqual(asked, [("http://adapter:19880/lan/hue-bridges",
                                  {"X-Adapter-Token": "private-adapter-key"})])

        def down(url, timeout, headers=None):
            raise OSError("adapter down")

        studio._read_local_json = down
        self.assertEqual(handler.lan_search("/lan/sync-boxes", lambda: ["local"])(), ["local"])

    def test_a_device_that_is_not_a_verified_bridge_is_not_found(self):
        self.serve(Bridge(answer_id="0000000000000000"))
        self.assertEqual(self.call("/api/hue-bridge/pair", {"ip": "192.168.10.5"}),
                         (503, {"error": "Hue bridge not found"}))


class PairedBridgeWatchTests(unittest.TestCase):
    """The adapter restarts after a pairing, but never while Sync owns the lamps."""

    def run_watch(self, change, busy):
        from live_adapter import watch_paired_bridge

        with TemporaryDirectory(prefix="tmp_hue_watch_") as folder:
            path = Path(folder) / "hue-bridge.json"
            stop = threading.Event()
            ticks = []

            class Stop:
                def wait(self, _):
                    ticks.append(1)
                    if len(ticks) == 2:
                        change(path)
                    return stop.is_set() or len(ticks) > 6

                def set(self):
                    stop.set()

                def is_set(self):
                    return stop.is_set()

            watch_paired_bridge(path, Stop(), busy, interval=0)
            return stop.is_set(), len(ticks)

    def test_a_new_pairing_restarts_the_adapter(self):
        self.assertEqual(self.run_watch(lambda path: path.write_text("{}"), lambda: False),
                         (True, 2))

    def test_no_change_or_active_sync_keeps_the_adapter_running(self):
        self.assertFalse(self.run_watch(lambda path: None, lambda: False)[0])
        self.assertFalse(self.run_watch(lambda path: path.write_text("{}"), lambda: True)[0])

if __name__ == "__main__":
    unittest.main()
