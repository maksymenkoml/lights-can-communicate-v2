"""A source that re-handshakes from a new port must not stay fenced by a retained session.

Synthetic loopback with real OpenSSL DTLS and the production UDP guard.
"""

import shutil
import socket
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "experiments/hue-bridge"))
import test_receiver_udp_guard as guarded  # noqa: E402
from test_receiver_recovery import wait_for  # noqa: E402


@unittest.skipUnless(sys.platform == "linux", "UDP guard requires Linux shared-bind routing")
@unittest.skipUnless(shutil.which("openssl"), "OpenSSL required for loopback DTLS")
class ReceiverReconnectTests(unittest.TestCase):
    guard = guarded.ReceiverUdpGuardTests.guard
    guarded_loop = guarded.ReceiverUdpGuardTests.guarded_loop

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
        self.assertEqual(loop.overlapping_listeners, 0)

    def test_pause_on_the_established_connection_is_retained(self):
        guard, loop = self.guarded_loop()
        connection = loop.start()
        loop.frame(connection, 11111)
        wait_for(lambda: len(loop.frames()) == 1)
        process = loop.group.stream["_proc"]
        # A stray datagram that is not a ClientHello must not end the session.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as other:
            other.sendto(b"\x17\xfe\xfd" + bytes(20), guard.address)
        wait_for(lambda: guard.snapshot()["discarded_datagrams"] >= 1)
        time.sleep(2.5)
        self.assertIs(loop.group.stream["_proc"], process)
        loop.frame(connection, 22222)
        wait_for(lambda: len(loop.frames()) == 2)
        self.assertNotIn("reconnects", loop.receiver.snapshot())

    def test_stop_during_reconnect_does_not_listen_again(self):
        guard, loop = self.guarded_loop()
        first = loop.start()
        loop.frame(first, 11111)
        wait_for(lambda: len(loop.frames()) == 1)
        first.process.kill()
        first.process.wait(2)
        loop.group.stream["active"] = False
        loop.threads[-1].join(4)
        self.assertFalse(loop.threads[-1].is_alive())
        self.assertFalse(loop.receiver.snapshot()["active"])


if __name__ == "__main__":
    unittest.main()
