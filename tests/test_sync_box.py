"""Sync Box control: discovery parsing, pairing, Start/Stop, pinning and the studio routes."""

import importlib.util
import json
import os
import stat
import struct
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge"))
from sync_box import (  # noqa: E402
    BUTTON_NOT_PRESSED,
    CertificateMismatch,
    SyncBox,
    box_address,
    mdns_answers_service,
    mdns_query,
)

spec = importlib.util.spec_from_file_location(
    "setup_studio_sync_box", Path(__file__).resolve().parents[1] / "studio/server.py")
studio = importlib.util.module_from_spec(spec)
spec.loader.exec_module(studio)

PIN = "a" * 64
TOKEN = "secret-token-value"


class FakeBox:
    """The Sync Box API as Light Studio uses it, at one address with one certificate."""

    def __init__(self, ip="192.168.10.101", fingerprint=PIN):
        self.ip, self.fingerprint = ip, fingerprint
        self.button = False
        self.syncing = False
        self.tokens = set()
        self.calls = []
        self.online = True

    def __call__(self, ip, method, path, body=None, token=None, pin=None, timeout=3.0):
        self.calls.append((ip, method, path, body, token, pin))
        if ip != self.ip or not self.online:
            raise OSError("unreachable")
        if pin is not None and pin != self.fingerprint:
            raise CertificateMismatch("other certificate")
        if path == "/api/v1/device":
            return 200, {"name": "Sync Box 8K", "deviceType": "HSB2",
                         "uniqueId": "C42996E1B244"}, self.fingerprint
        if path == "/api/v1/registrations" and method == "POST":
            if not self.button:
                refusal = {"code": BUTTON_NOT_PRESSED, "message": "Invalid State"}
                return 400, refusal, self.fingerprint
            self.tokens.add(TOKEN)
            return 200, {"registrationId": "7", "accessToken": TOKEN}, self.fingerprint
        if token not in self.tokens:
            return 401, {}, self.fingerprint
        if path == "/api/v1/execution" and method == "GET":
            return 200, {"syncActive": self.syncing, "mode": "video"}, self.fingerprint
        if path == "/api/v1/execution" and method == "PUT":
            self.syncing = body["syncActive"]
            return 200, {}, self.fingerprint
        if path == "/api/v1/registrations/7" and method == "DELETE":
            self.tokens.discard(token)
            return 200, {}, self.fingerprint
        return 404, {}, self.fingerprint


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        time.sleep(0.001)


def response(*records, flags=0x8400):
    """A DNS response with (name, type, rdata) answer records; names are uncompressed."""
    def name(text):
        return b"".join(bytes([len(p)]) + p.encode() for p in text.split(".")) + b"\0"
    body = b"".join(name(n) + struct.pack(">HHIH", kind, 1, 120, len(data)) + data
                    for n, kind, data in records)
    return struct.pack(">HHHHHH", 0, flags, 0, len(records), 0, 0) + body


class DiscoveryTests(unittest.TestCase):
    def test_the_query_asks_for_the_sync_box_service(self):
        packet = mdns_query()
        self.assertEqual(struct.unpack(">HHHHHH", packet[:12]), (0, 0, 1, 0, 0, 0))
        self.assertIn(b"\x08_huesync\x04_tcp\x05local\x00", packet)
        self.assertEqual(packet[-4:], struct.pack(">HH", 12, 1))

    def test_a_response_naming_the_service_is_a_sync_box(self):
        target = b"\x03box" + b"\xc0\x0c"  # "box." plus a pointer to the record name
        packet = response(("_huesync._tcp.local", 12, target))
        self.assertTrue(mdns_answers_service(packet))

    def test_other_services_queries_and_garbage_are_not(self):
        self.assertFalse(mdns_answers_service(response(("_hue._tcp.local", 12, b"\x00"))))
        self.assertFalse(mdns_answers_service(response(("_huesync._tcp.local", 12, b"\x00"),
                                                       flags=0)))
        self.assertFalse(mdns_answers_service(b"\x00" * 5))
        # A pointer loop must not hang.
        loop = struct.pack(">HHHHHH", 0, 0x8400, 0, 1, 0, 0) + b"\xc0\x0c"
        self.assertFalse(mdns_answers_service(loop))

    def test_only_local_network_addresses_are_accepted(self):
        self.assertEqual(box_address(" 192.168.10.101 "), "192.168.10.101")
        for value in ("8.8.8.8", "127.0.0.1", "::1", "box.local", "", None, 7, "1" * 40):
            with self.assertRaises(ValueError, msg=value):
                box_address(value)


class SyncBoxTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory(prefix="tmp_sync_box_")
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "sync-box.json"
        self.box = FakeBox()
        self.clock = Clock()
        self.found = [self.box.ip]
        self.control = SyncBox(self.path, transport=self.box, find=lambda: list(self.found),
                               clock=self.clock, sleep=self.clock.sleep, background=False)

    def pair(self):
        self.box.button = True
        self.control.pair(self.box.ip)
        self.wait_for(lambda: self.path.exists())

    def wait_for(self, condition):
        end = time.monotonic() + 3
        while not condition():
            self.assertLess(time.monotonic(), end, "timed out")
            time.sleep(0.005)

    def test_without_a_box_the_state_is_none(self):
        self.assertEqual(self.control.status(), {"state": "none"})

    def test_find_lists_the_boxes_that_answer_as_sync_boxes(self):
        self.found = [self.box.ip, "192.168.10.9", "8.8.8.8"]
        self.assertEqual(self.control.find(),
                         {"boxes": [{"ip": "192.168.10.101", "name": "Sync Box 8K"}]})

    def test_pairing_waits_for_the_button_then_stores_a_private_token(self):
        status = self.control.pair(self.box.ip)
        self.assertEqual(status["state"], "connecting")
        # The first request may already have waited a second.
        self.assertIn(status["remaining"], (29, 30))
        self.wait_for(lambda: len(self.box.calls) >= 3)
        self.assertFalse(self.path.exists())
        self.box.button = True
        self.wait_for(lambda: self.path.exists())
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        saved = json.loads(self.path.read_text())
        self.assertEqual((saved["accessToken"], saved["fingerprint"], saved["uniqueId"]),
                         (TOKEN, PIN, "C42996E1B244"))
        registration = [c for c in self.box.calls if c[2] == "/api/v1/registrations"][-1]
        self.assertEqual(registration[3]["appName"], "lightscan")
        self.assertEqual(registration[5], PIN, "registration is pinned to the described box")
        status = self.control.status()
        self.assertEqual(status, {"state": "ready", "syncing": False,
                                  "name": "Sync Box 8K", "ip": "192.168.10.101"})
        self.assertNotIn(TOKEN, json.dumps(status))

    def test_pairing_times_out_and_cancel_clears_it(self):
        self.control.pair_seconds = 3
        self.control.pair(self.box.ip)
        self.wait_for(lambda: self.control.status()["state"] == "timeout")
        self.assertFalse(self.path.exists())
        self.assertEqual(self.control.cancel(), {"state": "none"})

    def test_a_device_that_is_not_a_sync_box_is_refused(self):
        with self.assertRaises(ValueError):
            self.control.pair("8.8.8.8")
        with self.assertRaises(studio.BoxError):
            self.control.pair("192.168.10.9")

    def test_start_and_stop_sync(self):
        self.pair()
        self.assertEqual(self.control.set_sync(True)[0], 200)
        self.assertTrue(self.box.syncing)
        code, status = self.control.set_sync(False)
        self.assertEqual((code, status["syncing"]), (200, False))
        put = [c for c in self.box.calls if c[1] == "PUT"][-1]
        self.assertEqual((put[3], put[4], put[5]), ({"syncActive": False}, TOKEN, PIN))

    def test_start_without_a_box_is_refused(self):
        self.assertEqual(self.control.set_sync(True), (409, {"state": "none"}))

    def test_a_revoked_token_asks_to_reconnect(self):
        self.pair()
        self.box.tokens.clear()
        self.clock.now += 5
        self.assertEqual(self.control.status()["state"], "unauthorized")
        self.assertEqual(self.control.set_sync(True)[0], 409)

    def test_another_certificate_at_the_address_gets_no_token(self):
        self.pair()
        self.box.fingerprint = "b" * 64
        self.clock.now += 20  # past the grace of an answer pair() may have read
        sent = len(self.box.calls)
        self.assertEqual(self.control.status()["state"], "offline")
        self.assertTrue(all(c[4] is None or c[5] == PIN for c in self.box.calls[sent:]))

    def test_a_box_that_moved_is_followed_by_its_id_and_certificate(self):
        self.pair()
        self.box.ip = "192.168.10.120"
        self.found = ["192.168.10.120"]
        self.clock.now += 5
        status = self.control.status()
        self.assertEqual((status["state"], status["ip"]), ("ready", "192.168.10.120"))
        self.assertEqual(json.loads(self.path.read_text())["ip"], "192.168.10.120")

    def test_an_unreachable_box_is_offline_and_searched_at_most_every_30_s(self):
        self.pair()
        self.box.online = False
        searches = []
        self.control.find_addresses = lambda: searches.append(1) or []
        self.clock.now += 20  # past the grace of an answer pair() may have read
        self.assertEqual(self.control.status()["state"], "offline")
        for _ in range(5):
            self.clock.now += 5
            self.assertEqual(self.control.status()["state"], "offline")
        self.assertEqual(len(searches), 1)

    def test_a_slow_answer_keeps_the_box_until_it_stays_silent_for_15_s(self):
        self.pair()
        self.assertEqual(self.control.status()["state"], "ready")
        self.box.online = False
        self.found = []
        for _ in range(2):
            self.clock.now += 5
            self.assertEqual(self.control.status()["state"], "ready")
        self.clock.now += 6
        self.assertEqual(self.control.status()["state"], "offline")
        self.box.online = True
        self.clock.now += 5
        self.assertEqual(self.control.status()["state"], "ready")

    def test_every_poll_shows_the_change_until_the_box_answers(self):
        self.pair()
        self.control.status()
        answered, release = threading.Event(), threading.Event()
        plain = self.box.__call__

        def slow(ip, method, path, body=None, token=None, pin=None, timeout=3.0):
            if method == "PUT":
                self.assertEqual(timeout, 15.0)
                answered.set()
                release.wait(2)
            return plain(ip, method, path, body, token, pin, timeout)

        self.control.transport = slow
        result = {}
        worker = threading.Thread(target=lambda: result.update(code=self.control.set_sync(True)))
        worker.start()
        answered.wait(2)
        status = self.control.status()
        self.assertEqual((status["syncing"], status["changing"]), (False, True))
        self.assertEqual(self.control.set_sync(False)[0], 409, "one change at a time")
        release.set()
        worker.join(2)
        code, status = result["code"]
        self.assertEqual((code, status["syncing"]), (200, True))
        self.assertNotIn("changing", status)

    def test_polls_answer_at_once_while_a_slow_read_runs(self):
        self.pair()
        self.control.status()
        self.control.background = True
        started, release = threading.Event(), threading.Event()
        plain = self.box.__call__

        def slow(*args, **kwargs):
            started.set()
            release.wait(2)
            return plain(*args, **kwargs)

        self.control.transport = slow
        self.box.syncing = True
        self.clock.now += 5
        self.assertEqual(self.control.status()["syncing"], False, "the last read answers")
        started.wait(2)
        self.assertEqual(self.control.status()["syncing"], False, "one read at a time")
        release.set()
        self.wait_for(lambda: self.control.status()["syncing"] is True)

    def test_forget_removes_the_registration_and_the_file(self):
        self.pair()
        self.assertEqual(self.control.forget(), {"state": "none"})
        self.assertFalse(self.path.exists())
        self.assertEqual(self.box.tokens, set())

    def test_forget_works_while_the_box_is_offline(self):
        self.pair()
        self.box.online = False
        self.assertEqual(self.control.forget(), {"state": "none"})
        self.assertFalse(self.path.exists())


class StudioSyncBoxRouteTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory(prefix="tmp_studio_sync_box_")
        self.addCleanup(temp.cleanup)
        folder = Path(temp.name)
        self.box = FakeBox()
        self.control = SyncBox(folder / "sync-box.json", transport=self.box,
                               find=lambda: [self.box.ip], background=False)
        server = ThreadingHTTPServer(("127.0.0.1", 0), studio.handler_for(
            folder / "missing-token", folder / "desk-layout.json", sync_box=self.control))
        worker = threading.Thread(target=server.serve_forever)
        worker.start()
        self.base = "http://127.0.0.1:" + str(server.server_port)

        def close():
            self.control.cancel()
            server.shutdown()
            server.server_close()
            worker.join(2)

        self.addCleanup(close)

    def call(self, path, body=None, **headers):
        data = body if isinstance(body, bytes) or body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            self.base + path, data=data, method="GET" if body is None else "POST",
            headers={"Content-Type": "application/json", "Origin": self.base, **headers})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=5) as answer:
                return answer.status, json.load(answer)
        except urllib.error.HTTPError as answer:
            with answer:
                return answer.code, json.load(answer)

    def test_the_owner_connects_starts_and_forgets_a_box(self):
        self.assertEqual(self.call("/api/sync-box"), (200, {"state": "none"}))
        self.assertEqual(self.call("/api/sync-box/find", {}),
                         (200, {"boxes": [{"ip": "192.168.10.101", "name": "Sync Box 8K"}]}))
        self.box.button = True
        code, status = self.call("/api/sync-box/pair", {"ip": "192.168.10.101"})
        # The button is already held, so the box may answer before this response.
        self.assertEqual(code, 200)
        self.assertIn(status["state"], ("connecting", "ready"))
        end = time.monotonic() + 3
        while self.call("/api/sync-box")[1]["state"] != "ready":
            self.assertLess(time.monotonic(), end)
            time.sleep(0.02)
        code, status = self.call("/api/sync-box/sync", {"active": True})
        self.assertEqual((code, status["syncing"]), (200, True))
        self.assertNotIn(TOKEN, json.dumps(status))
        self.assertNotIn(PIN, json.dumps(status))
        self.assertEqual(self.call("/api/sync-box/forget", {}), (200, {"state": "none"}))

    def test_bad_requests_are_refused(self):
        self.assertEqual(self.call("/api/sync-box/pair", {"ip": "8.8.8.8"})[0], 400)
        self.assertEqual(self.call("/api/sync-box/pair", {"ip": "192.168.10.9"})[0], 503)
        self.assertEqual(self.call("/api/sync-box/pair", {})[0], 400)
        self.assertEqual(self.call("/api/sync-box/sync", {"active": "yes"})[0], 400)
        self.assertEqual(self.call("/api/sync-box/sync", {"active": True})[0], 409)
        self.assertEqual(self.call("/api/sync-box/forget", {"x": 1})[0], 400)
        self.assertEqual(self.call("/api/sync-box/forget", b"[]")[0], 400)
        self.assertEqual(self.call("/api/sync-box/forget", b"{" + b" " * 300 + b"}")[0], 413)

    def test_other_origins_cannot_control_the_box(self):
        code, _ = self.call("/api/sync-box/sync", {"active": True}, Origin="http://evil.test")
        self.assertEqual(code, 403)
        self.assertEqual(self.call("/api/sync-box", Origin="http://evil.test")[0], 403)


if __name__ == "__main__":
    unittest.main()
