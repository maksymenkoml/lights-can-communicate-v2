"""A source that re-handshakes from a new port must not stay fenced by a retained session.

Synthetic loopback with real OpenSSL DTLS and the production UDP guard.
"""

import shutil
import socket
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge"))
import test_receiver_udp_guard as guarded  # noqa: E402
from test_receiver_recovery import wait_for  # noqa: E402


@unittest.skipUnless(sys.platform == "linux", "UDP guard requires Linux shared-bind routing")
@unittest.skipUnless(shutil.which("openssl"), "OpenSSL required for loopback DTLS")
class ReceiverReconnectTests(unittest.TestCase):
    guard = guarded.ReceiverUdpGuardTests.guard
    guarded_loop = guarded.ReceiverUdpGuardTests.guarded_loop

    def forged_hello(self, guard):
        # Shaped like a DTLS 1.2 ClientHello, but from a peer without the key.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as other:
            other.sendto(b"\x16\xfe\xfd" + bytes(10) + b"\x01" + bytes(40), guard.address)

    def test_vanished_client_then_new_handshake_resumes_frames(self):
        guard, loop = self.guarded_loop()
        first = loop.start()
        loop.frame(first, 11111)
        wait_for(lambda: len(loop.frames()) == 1)
        # The source disappears without close_notify (Wi-Fi drop, Box restart).
        first.process.kill()
        first.process.wait(2)
        # It reconnects with a new DTLS session from a new port, without a new REST Start.
        second = loop.connect()
        loop.frame(second, 22222)
        wait_for(lambda: len(loop.frames()) == 2, timeout=15)
        self.assertEqual(loop.frames()[-1]["colors"][0], [0, 22222, 0, 0])
        status = loop.receiver.snapshot()
        self.assertEqual(status["reconnects"], 1)
        self.assertTrue(loop.group.stream["active"])
        self.assertGreaterEqual(guard.snapshot()["client_hellos"], 1)
        events = [(e["state"], e["reason"]) for e in status["diagnostics"]["events"]]
        self.assertIn(("waiting", "source_reconnected"), events)
        # Exactly the old association and its replacement; the old one is gone.
        self.assertEqual(len(loop.servers), 2)
        wait_for(lambda: loop.servers[0].poll() is not None)
        loop.frame(second, 33333)
        wait_for(lambda: len(loop.frames()) == 3)

    def test_hello_while_streaming_does_not_end_a_later_pause(self):
        guard, loop = self.guarded_loop()
        connection = loop.start()
        loop.frame(connection, 11111)
        wait_for(lambda: len(loop.frames()) == 1)
        self.forged_hello(guard)
        wait_for(lambda: guard.snapshot()["client_hellos"] == 1)
        loop.frame(connection, 22222)
        wait_for(lambda: len(loop.frames()) == 2)
        time.sleep(1.5)
        self.assertEqual(len(loop.servers), 1)
        loop.frame(connection, 33333)
        wait_for(lambda: len(loop.frames()) == 3)
        self.assertNotIn("reconnects", loop.receiver.snapshot())

    def test_unauthenticated_hello_during_a_pause_keeps_the_association(self):
        guard, loop = self.guarded_loop()
        connection = loop.start()
        loop.frame(connection, 11111)
        wait_for(lambda: len(loop.frames()) == 1)
        process = loop.group.stream["_proc"]
        time.sleep(0.7)
        self.forged_hello(guard)
        wait_for(lambda: len(loop.servers) == 2)
        # The forged peer never authenticates; the paused source resumes.
        loop.frame(connection, 22222)
        wait_for(lambda: len(loop.frames()) == 2)
        self.assertIs(loop.group.stream["_proc"], process)
        wait_for(lambda: loop.servers[1].poll() is not None)
        self.assertNotIn("reconnects", loop.receiver.snapshot())
        self.assertEqual(loop.frames()[-1]["colors"][0], [0, 22222, 0, 0])

    def test_silent_source_ends_the_session_so_the_source_can_start_again(self):
        guard, loop = self.guarded_loop()
        connection = loop.start()
        loop.frame(connection, 11111)
        wait_for(lambda: len(loop.frames()) == 1)
        # The source vanished without close_notify and sends nothing (Box reboot).
        with patch("entertainment_receiver.STREAM_TIMEOUT", 1.5):
            loop.threads[-1].join(5)
        self.assertFalse(loop.threads[-1].is_alive())
        self.assertFalse(loop.group.stream["active"])
        status = loop.receiver.snapshot()
        self.assertEqual(status["last_result"], "source timeout")
        events = [(e["state"], e["reason"]) for e in status["diagnostics"]["events"]]
        self.assertIn(("idle", "source_timeout"), events)
        # The source's new Start is served normally.
        again = loop.start()
        loop.frame(again, 22222)
        wait_for(lambda: len(loop.frames()) == 2)

    def test_replacement_spawn_failure_keeps_the_association(self):
        guard, loop = self.guarded_loop()
        connection = loop.start()
        loop.frame(connection, 11111)
        wait_for(lambda: len(loop.frames()) == 1)
        process = loop.group.stream["_proc"]

        def exhausted():
            raise OSError("Synthetic process limit")

        loop.before_spawn = exhausted
        time.sleep(0.7)
        self.forged_hello(guard)
        time.sleep(0.5)
        loop.frame(connection, 22222)
        wait_for(lambda: len(loop.frames()) == 2)
        self.assertIs(loop.group.stream["_proc"], process)
        self.assertTrue(loop.receiver.snapshot()["active"])

    def test_stop_while_a_replacement_listens_leaves_no_listener(self):
        guard, loop = self.guarded_loop()
        connection = loop.start()
        loop.frame(connection, 11111)
        wait_for(lambda: len(loop.frames()) == 1)
        time.sleep(0.7)
        self.forged_hello(guard)
        wait_for(lambda: len(loop.servers) == 2)
        loop.group.stream["active"] = False
        loop.threads[-1].join(4)
        self.assertFalse(loop.threads[-1].is_alive())
        self.assertTrue(all(server.poll() is not None for server in loop.servers))
        self.assertFalse(loop.receiver.snapshot()["active"])

if __name__ == "__main__":
    unittest.main()
