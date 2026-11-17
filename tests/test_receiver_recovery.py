"""Real loopback DTLS/HTTP faults; no physical adapter or credentials involved."""

import json
import os
import select
import shutil
import socket
import subprocess
import sys
import threading
import time
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "experiments/hue-bridge"))
from entertainment_receiver import EntertainmentReceiver  # noqa: E402
from entertainment_stream import AREA_ID, CHANNELS, GROUP_ID, encode_frame  # noqa: E402
from hue_entertainment_relay import DtlsConnection  # noqa: E402


def wait_for(predicate, timeout=4):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("Loopback condition not reached")


def wait_for_listener(process, timeout=4, limit=4096):
    """Consume only OpenSSL's bounded preamble, leaving application bytes intact."""
    deadline = time.monotonic() + timeout
    line = bytearray()
    for _ in range(limit):
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([process.stdout], [], [], remaining)[0]:
            raise TimeoutError("Loopback OpenSSL listener did not become ready")
        byte = os.read(process.stdout.fileno(), 1)
        if not byte:
            # Return failed listeners so the receiver can collect their stderr.
            return False
        line.extend(byte)
        if byte == b"\n":
            if line == b"ACCEPT\n":
                return True
            line.clear()
    raise ValueError("Loopback OpenSSL readiness preamble exceeded its limit")


class ListenerReadinessTests(unittest.TestCase):
    def child(self, script):
        process = subprocess.Popen(
            [sys.executable, "-c", script], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

        def close():
            if process.poll() is None:
                process.terminate()
            try:
                process.communicate(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=1)

        self.addCleanup(close)
        return process

    def test_exact_ready_marker_leaves_first_application_bytes_for_receiver(self):
        process = self.child(
            "import os,sys; os.write(1,b'NOTACCEPT\\nACCEPT\\nHueStream-payload'); "
            "sys.stdin.buffer.read(1)"
        )
        self.assertTrue(wait_for_listener(process))
        self.assertTrue(select.select([process.stdout], [], [], 1)[0])
        self.assertEqual(os.read(process.stdout.fileno(), 17), b"HueStream-payload")

    def test_failed_listener_eof_preserves_bind_diagnostic_for_receiver(self):
        process = self.child("import os; os.write(2,b'Address already in use')")
        self.assertFalse(wait_for_listener(process))
        self.assertEqual(process.stderr.read(), b"Address already in use")

    def test_silent_listener_has_bounded_readiness_wait(self):
        process = self.child("import sys; sys.stdin.buffer.read(1)")
        with self.assertRaises(TimeoutError):
            wait_for_listener(process, timeout=0.02)

    def test_noisy_listener_has_bounded_preamble(self):
        process = self.child("import os,sys; os.write(1,b'x'*17); sys.stdin.buffer.read(1)")
        with self.assertRaises(ValueError):
            wait_for_listener(process, limit=16)


class TestGroup:
    def __init__(self):
        self.id_v2 = GROUP_ID
        self.members = [types.SimpleNamespace(id_v1=str(i)) for i in range(1, 7)]
        # SimpleNamespace cannot be weak-referenced; callables mirror the vendor boundary.
        self.lights = [lambda member=member: member for member in self.members]
        self.stream = {"active": True}

    def update_attr(self, values):
        self.stream.update(values["stream"])


class LoopbackReceiver:
    def __init__(self):
        self.messages = []
        self.reject_http = False
        self.ack = {"accepted": True}
        self.clients = []
        self.threads = []
        self.servers = []
        self.overlapping_listeners = 0
        self.before_spawn = None
        self.server_start_delay = 0
        harness = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if harness.reject_http:
                    self.send_error(503)
                    return
                harness.messages.append(body)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(harness.ack).encode())

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.http_thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.http_thread.start()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as reserve:
            reserve.bind(("127.0.0.1", 0))
            self.port = reserve.getsockname()[1]
        self.group = TestGroup()
        self.user = types.SimpleNamespace(client_key="01" * 16, username="loopback-test")
        vendor = types.ModuleType("services.entertainment")
        vendor._psk_value = lambda value: value
        self.modules = patch.dict(sys.modules, {"services.entertainment": vendor})
        self.modules.start()
        original_popen = subprocess.Popen

        def loopback_popen(args, **kwargs):
            args = list(args)
            if "s_server" in args:
                if self.before_spawn is not None:
                    self.before_spawn()
                self.overlapping_listeners += int(any(p.poll() is None for p in self.servers))
                args[args.index("-accept") + 1] = f"127.0.0.1:{self.port}"
                # OpenSSL emits and flushes ACCEPT after bind when not quiet.
                args = [arg for arg in args if arg != "-quiet"]
                if self.server_start_delay:
                    # Fault injection: process creation precedes the real UDP bind.
                    args = [sys.executable, "-c",
                            "import os,sys,time; time.sleep(float(sys.argv[1])); "
                            "os.execvp(sys.argv[2],sys.argv[2:])",
                            str(self.server_start_delay), *args]
            elif "s_client" in args:
                args[args.index("-connect") + 1] = f"127.0.0.1:{self.port}"
            else:
                raise AssertionError("Unexpected process in isolated transport check")
            process = original_popen(args, **kwargs)
            if "s_server" in args:
                self.servers.append(process)
                try:
                    wait_for_listener(process)
                except (OSError, ValueError):
                    process.terminate()
                    try:
                        process.communicate(timeout=1)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.communicate(timeout=1)
                    raise
            return process

        self.popen = patch("subprocess.Popen", side_effect=loopback_popen)
        self.popen.start()
        self.receiver = EntertainmentReceiver(
            f"http://127.0.0.1:{self.http.server_port}", "synthetic-token", enabled=True
        )

    def start_worker(self):
        self.group.update_attr({"stream": {"active": True, "owner": self.user.username}})
        worker = threading.Thread(target=self.receiver.serve, args=(self.group, self.user))
        self.threads.append(worker)
        worker.start()
        return worker

    def start(self):
        previous = self.group.stream.get("_proc")
        self.start_worker()
        wait_for(lambda: self.receiver.snapshot()["active"]
                 and self.group.stream.get("_proc") is not None
                 and self.group.stream["_proc"] is not previous)
        return self.connect()

    def connect(self):
        connection = DtlsConnection(
            {"client_key": self.user.client_key, "hue_key": self.user.username,
             "bridge": "127.0.0.1"}
        )
        self.clients.append(connection)
        return connection

    def frame(self, connection, value=12345):
        connection.send(encode_frame(AREA_ID, {i: (value, 0, 0) for i in CHANNELS}, 1))

    def frames(self):
        return [message for message in self.messages if message["action"] == "frame"]

    def close(self):
        self.group.stream["active"] = False
        for connection in self.clients:
            connection.close()
        for worker in self.threads:
            worker.join(4)
        self.popen.stop()
        self.modules.stop()
        self.http.shutdown()
        self.http.server_close()
        self.http_thread.join(2)
        if any(worker.is_alive() for worker in self.threads):
            raise AssertionError("Receiver worker leaked after shutdown")


@unittest.skipUnless(shutil.which("openssl"), "OpenSSL required for loopback DTLS")
class ReceiverRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.loop = LoopbackReceiver()
        self.addCleanup(self.loop.close)

    def test_source_pause_longer_than_two_seconds_resumes_same_dtls_connection(self):
        connection = self.loop.start()
        self.loop.frame(connection)
        wait_for(lambda: len(self.loop.frames()) == 1)
        time.sleep(2.3)
        self.assertTrue(self.loop.receiver.snapshot()["active"], "Pause killed source connection")
        self.loop.frame(connection, 54321)
        wait_for(lambda: len(self.loop.frames()) == 2)
        self.assertEqual(self.loop.frames()[-1]["colors"][0], [0, 54321, 0, 0])
        self.assertEqual(self.loop.frames()[0]["session"], self.loop.frames()[1]["session"])

    def test_invalid_frame_does_not_destroy_dtls_or_discard_following_valid_frame(self):
        connection = self.loop.start()
        packet = encode_frame(AREA_ID, {i: (1000, 0, 0) for i in CHANNELS}, 1)
        # The malformed packet addresses a channel outside the selected desk.
        malformed = packet[:52] + bytes([255]) + packet[53:]
        connection.send(malformed + packet)
        wait_for(lambda: bool(self.loop.frames()))
        self.assertEqual(self.loop.frames()[0]["colors"][0], [0, 1000, 0, 0])
        self.assertTrue(self.loop.receiver.snapshot()["active"])

    def test_host_http_outage_recovers_on_fresh_frame_without_dtls_restart(self):
        connection = self.loop.start()
        self.loop.reject_http = True
        self.loop.frame(connection)
        wait_for(lambda: self.loop.receiver.snapshot()["outbox"]["last_http_status"] == 503)
        self.loop.reject_http = False
        self.loop.frame(connection, 44444)
        wait_for(lambda: bool(self.loop.frames()))
        self.assertEqual(len(self.loop.frames()), 1)
        self.assertEqual(self.loop.frames()[0]["colors"][0], [0, 44444, 0, 0])

    def test_explicit_stop_retires_stream_and_next_start_has_new_session(self):
        first = self.loop.start()
        self.loop.frame(first)
        wait_for(lambda: len(self.loop.frames()) == 1)
        self.loop.group.stream["active"] = False
        self.loop.threads[-1].join(3)
        self.assertFalse(self.loop.threads[-1].is_alive())
        wait_for(lambda: any(m["action"] == "stop" for m in self.loop.messages))
        second = self.loop.start()
        self.loop.frame(second, 50000)
        wait_for(lambda: len(self.loop.frames()) == 2)
        self.assertNotEqual(self.loop.frames()[0]["session"], self.loop.frames()[1]["session"])

    def test_overlapping_restart_waits_for_old_listener_cleanup(self):
        first = self.loop.start()
        self.loop.frame(first)
        wait_for(lambda: len(self.loop.frames()) == 1)
        second = self.loop.start()
        self.loop.frame(second, 22222)
        wait_for(lambda: len(self.loop.frames()) == 2)
        self.assertEqual(self.loop.frames()[-1]["colors"][0], [0, 22222, 0, 0])
        self.assertNotEqual(self.loop.frames()[0]["session"], self.loop.frames()[1]["session"])
        self.loop.threads[0].join(2)
        self.assertFalse(self.loop.threads[0].is_alive())
        self.assertEqual(self.loop.overlapping_listeners, 0, "Two live listeners shared UDP port")
        self.assertTrue(self.loop.receiver.snapshot()["active"])

    def test_explicit_stop_while_source_idle_releases_listener(self):
        connection = self.loop.start()
        self.loop.frame(connection)
        wait_for(lambda: len(self.loop.frames()) == 1)
        wait_for(lambda: "idle" in self.loop.receiver.snapshot()["last_result"])
        self.loop.group.stream["active"] = False
        self.loop.threads[-1].join(3)
        self.assertFalse(self.loop.threads[-1].is_alive())
        self.assertFalse(self.loop.receiver.snapshot()["active"])

    def test_delayed_restarted_listener_accepts_a_fresh_client_without_retry(self):
        first = self.loop.start()
        self.loop.frame(first, 11111)
        wait_for(lambda: len(self.loop.frames()) == 1)
        self.loop.server_start_delay = 0.15
        second = self.loop.start()
        self.loop.frame(second, 22222)
        wait_for(lambda: len(self.loop.frames()) == 2)
        self.assertEqual(self.loop.frames()[-1]["colors"][0], [0, 22222, 0, 0])
        self.assertNotEqual(self.loop.frames()[0]["session"], self.loop.frames()[1]["session"])
        self.assertEqual(self.loop.overlapping_listeners, 0)

    def test_killed_listener_can_be_started_again(self):
        connection = self.loop.start()
        self.loop.frame(connection)
        wait_for(lambda: len(self.loop.frames()) == 1)
        self.loop.group.stream["_proc"].kill()
        self.loop.threads[-1].join(3)
        self.assertFalse(self.loop.threads[-1].is_alive())
        second = self.loop.start()
        self.loop.frame(second, 33333)
        wait_for(lambda: len(self.loop.frames()) == 2)
        self.assertEqual(self.loop.frames()[-1]["colors"][0], [0, 33333, 0, 0])

    def test_failed_process_spawn_does_not_block_next_start(self):
        original = self.loop.receiver.openssl
        self.loop.receiver.openssl = "/nonexistent/synthetic-openssl"
        worker = self.loop.start_worker()
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertFalse(self.loop.receiver.snapshot()["active"])
        self.loop.receiver.openssl = original
        connection = self.loop.start()
        self.loop.frame(connection, 1234)
        wait_for(lambda: bool(self.loop.frames()))
        self.assertEqual(self.loop.frames()[0]["colors"][0], [0, 1234, 0, 0])

    def test_busy_udp_port_failure_releases_gate_for_next_start(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as occupied:
            occupied.bind(("127.0.0.1", self.loop.port))
            worker = self.loop.start_worker()
            worker.join(3)
            self.assertFalse(worker.is_alive())
            self.assertFalse(self.loop.receiver.snapshot()["active"])
            self.assertGreater(
                self.loop.receiver.snapshot()["dtls_diagnostics"].get("listener_bind_error", 0), 0
            )
        connection = self.loop.start()
        self.loop.frame(connection)
        wait_for(lambda: bool(self.loop.frames()))

    def test_wrong_psk_cannot_forward_colors_and_correct_restart_recovers(self):
        self.loop.start_worker()
        wait_for(lambda: self.loop.receiver.snapshot()["active"])
        with self.assertRaises(OSError):
            DtlsConnection({"client_key": "ff" * 16, "hue_key": "loopback-test",
                            "bridge": "127.0.0.1"})
        self.assertEqual(self.loop.frames(), [])
        connection = self.loop.start()
        self.loop.frame(connection, 32123)
        wait_for(lambda: bool(self.loop.frames()))
        self.assertEqual(self.loop.frames()[0]["colors"][0], [0, 32123, 0, 0])

    def test_start_accepted_before_delayed_worker_survives_old_cleanup(self):
        self.loop.receiver.bind(self.loop.group)
        first = self.loop.start()
        self.loop.frame(first)
        wait_for(lambda: len(self.loop.frames()) == 1)
        self.loop.group.update_attr({"stream": {"active": False}})
        self.loop.group.stream["_proc"].kill()
        self.loop.group.update_attr({"stream": {"active": True, "owner": self.loop.user.username}})
        self.loop.threads[0].join(3)
        self.assertTrue(self.loop.group.stream["active"], "Old cleanup erased accepted Start")
        second = self.loop.start()
        self.loop.frame(second, 12300)
        wait_for(lambda: len(self.loop.frames()) == 2)

    def test_stop_after_start_acceptance_cancels_delayed_worker(self):
        self.loop.receiver.bind(self.loop.group)
        self.loop.group.update_attr({"stream": {"active": True, "owner": self.loop.user.username}})
        self.loop.group.update_attr({"stream": {"active": False}})
        worker = threading.Thread(target=self.loop.receiver.serve,
                                  args=(self.loop.group, self.loop.user))
        self.loop.threads.append(worker)
        worker.start()
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.loop.servers, [])
        self.assertFalse(self.loop.group.stream["active"])

    def test_invalid_success_json_does_not_kill_http_forwarder(self):
        connection = self.loop.start()
        self.loop.ack = None
        self.loop.frame(connection)
        wait_for(lambda: self.loop.receiver.snapshot()["outbox"]["last_error"] is not None)
        self.loop.ack = {"accepted": True}
        self.loop.frame(connection, 65432)
        wait_for(lambda: self.loop.receiver.snapshot()["outbox"]["forwarded"] == 1)
        self.assertEqual(self.loop.frames()[-1]["colors"][0], [0, 65432, 0, 0])

    def test_duplicate_worker_for_same_accepted_start_does_not_replace_listener(self):
        self.loop.receiver.bind(self.loop.group)
        connection = self.loop.start()
        worker = threading.Thread(target=self.loop.receiver.serve,
                                  args=(self.loop.group, self.loop.user))
        self.loop.threads.append(worker)
        worker.start()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(self.loop.servers), 1)
        self.loop.frame(connection)
        wait_for(lambda: bool(self.loop.frames()))

    def test_superseded_spawn_cannot_overwrite_new_api_owner(self):
        entered, release = threading.Event(), threading.Event()

        def delayed_spawn():
            self.loop.before_spawn = None
            entered.set()
            release.wait(3)

        self.loop.receiver.bind(self.loop.group)
        self.loop.before_spawn = delayed_spawn
        self.loop.start_worker()
        self.assertTrue(entered.wait(2))
        self.loop.user = types.SimpleNamespace(client_key="01" * 16, username="new-loopback-user")
        self.loop.group.update_attr({"stream": {"active": True, "owner": self.loop.user.username}})
        release.set()
        self.loop.threads[0].join(3)
        self.assertEqual(self.loop.group.stream["owner"], "new-loopback-user")
        worker = threading.Thread(target=self.loop.receiver.serve,
                                  args=(self.loop.group, self.loop.user))
        self.loop.threads.append(worker)
        worker.start()
        wait_for(lambda: self.loop.group.stream.get("_proc") is not None
                 and self.loop.group.stream.get("owner") == "new-loopback-user")
        connection = self.loop.connect()
        self.loop.frame(connection, 55555)
        wait_for(lambda: bool(self.loop.frames()))
        self.assertEqual(self.loop.group.stream["owner"], "new-loopback-user")

    def test_two_queued_workers_for_one_start_leave_only_one_worker(self):
        entered, release = threading.Event(), threading.Event()

        def delayed_spawn():
            self.loop.before_spawn = None
            entered.set()
            release.wait(3)

        self.loop.receiver.bind(self.loop.group)
        self.loop.before_spawn = delayed_spawn
        self.loop.start_worker()
        self.assertTrue(entered.wait(2))
        self.loop.group.update_attr({"stream": {"active": True, "owner": self.loop.user.username}})
        queued = [threading.Thread(target=self.loop.receiver.serve,
                                   args=(self.loop.group, self.loop.user)) for _ in range(2)]
        for worker in queued:
            self.loop.threads.append(worker)
            worker.start()
        release.set()
        wait_for(lambda: len(self.loop.servers) == 2)
        wait_for(lambda: sum(worker.is_alive() for worker in queued) == 1)
        self.assertEqual(self.loop.overlapping_listeners, 0)
