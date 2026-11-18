"""Synthetic loopback only: persistent idle UDP bind beside real OpenSSL DTLS."""

import shutil
import socket
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge"))
from entertainment_receiver import EntertainmentReceiver  # noqa: E402
from test_receiver_recovery import LoopbackReceiver, wait_for  # noqa: E402
from udp_port_guard import UdpPortGuard  # noqa: E402


class UdpGuardPlatformTests(unittest.TestCase):
    def test_unsupported_platform_fails_before_creating_socket_or_worker(self):
        for platform in ("darwin", "win32"):
            with self.subTest(platform=platform):
                guard = UdpPortGuard(host="127.0.0.1", port=0)
                self.addCleanup(guard.close)
                with patch("sys.platform", platform), patch(
                    "udp_port_guard.socket.socket",
                    side_effect=AssertionError("Unsupported guard created a socket"),
                ) as socket_factory:
                    with self.assertRaisesRegex(RuntimeError, "requires Linux"):
                        guard.start()
                    socket_factory.assert_not_called()
                self.assertIsNone(guard.sock)
                self.assertIsNone(guard.worker)
                self.assertFalse(guard.snapshot()["running"])
                self.assertFalse(any(t.name == guard.worker_name for t in threading.enumerate()))
                guard.close()


@unittest.skipUnless(sys.platform == "linux", "UDP guard requires Linux shared-bind routing")
class ReceiverUdpGuardTests(unittest.TestCase):
    def guard(self, **kwargs):
        guard = UdpPortGuard(**kwargs)
        self.addCleanup(guard.close)
        return guard

    def guarded_loop(self, host="127.0.0.1"):
        # Register receiver cleanup last: its OpenSSL worker must exit before guard.close().
        guard = self.guard(host=host, port=0)
        guard.start()
        loop = LoopbackReceiver()
        self.addCleanup(loop.close)
        loop.port = guard.address[1]
        loop.receiver = EntertainmentReceiver(
            loop.receiver.url, loop.receiver.token, enabled=True, udp_guard=guard
        )
        return guard, loop

    def test_idle_port_is_bound_drains_datagrams_and_closes_without_worker(self):
        guard = self.guard(host="127.0.0.1", port=0)
        guard.start()
        guard.start()
        address = guard.address
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            for _ in range(6):
                sender.sendto(b"synthetic-idle-nonce", address)
        wait_for(lambda: guard.snapshot()["discarded_datagrams"] == 6)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as occupied:
            with self.assertRaises(OSError):
                occupied.bind(address)
        guard.close()
        guard.close()
        self.assertFalse(guard.snapshot()["running"])
        self.assertFalse(any(t.name == guard.worker_name for t in threading.enumerate()))
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as released:
            released.bind(address)

    def test_guard_never_rebinds_after_close_or_silently_ignores_occupied_port(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as occupied:
            occupied.bind(("127.0.0.1", 0))
            guard = self.guard(host="127.0.0.1", port=occupied.getsockname()[1])
            with self.assertRaises(OSError):
                guard.start()
            self.assertFalse(guard.snapshot()["running"])
        guard.start()
        guard.close()
        with self.assertRaises(ValueError):
            guard.start()
        with self.assertRaises(ValueError):
            _ = guard.accept_address

    def test_worker_start_failure_closes_socket_and_supports_retry_or_close(self):
        socket_factory = socket.socket
        for retry in (False, True):
            with self.subTest(retry=retry):
                created = []

                def track_socket(*args, **kwargs):
                    sock = socket_factory(*args, **kwargs)
                    created.append(sock)
                    self.addCleanup(sock.close)
                    return sock

                guard = self.guard(host="127.0.0.1", port=0)
                with patch("udp_port_guard.socket.socket", side_effect=track_socket), patch(
                    "udp_port_guard.threading.Thread.start",
                    side_effect=RuntimeError("Synthetic worker startup failure"),
                ):
                    with self.assertRaisesRegex(RuntimeError, "Synthetic worker startup failure"):
                        guard.start()
                self.assertEqual(len(created), 1)
                self.assertEqual(created[0].fileno(), -1, "Failed startup leaked its bound socket")
                self.assertIsNone(guard.sock)
                self.assertIsNone(guard.worker)
                self.assertFalse(guard.snapshot()["running"])
                if retry:
                    guard.start()
                    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
                        sender.sendto(b"synthetic-retry-packet", guard.address)
                    wait_for(lambda: guard.snapshot()["discarded_datagrams"] == 1)
                guard.close()
                guard.close()
                self.assertFalse(guard.snapshot()["running"])
                self.assertFalse(any(t.name == guard.worker_name for t in threading.enumerate()))

    @unittest.skipUnless(shutil.which("openssl"), "OpenSSL required for loopback DTLS")
    def test_repeated_start_stop_with_idle_datagrams_keeps_authenticated_frames(self):
        # Exercise the production wildcard bind, while clients remain on isolated loopback.
        guard, loop = self.guarded_loop(host="0.0.0.0")
        for cycle in range(3):
            before = guard.snapshot()["discarded_datagrams"]
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
                for _ in range(6):
                    sender.sendto(b"synthetic-late-dtls", guard.address)
            wait_for(lambda: guard.snapshot()["discarded_datagrams"] == before + 6)
            connection = loop.start()
            loop.frame(connection, 12000 + cycle)
            wait_for(lambda: len(loop.frames()) == cycle + 1)
            loop.group.stream["active"] = False
            loop.threads[-1].join(3)
            self.assertFalse(loop.threads[-1].is_alive())
            connection.close()
            self.assertTrue(guard.snapshot()["running"])
        self.assertEqual(loop.overlapping_listeners, 0)
        self.assertEqual(len({m["session"] for m in loop.frames()}), 3)

    @unittest.skipUnless(shutil.which("openssl"), "OpenSSL required for loopback DTLS")
    def test_guard_does_not_authenticate_wrong_psk_and_restart_recovers(self):
        from hue_entertainment_relay import DtlsConnection

        _, loop = self.guarded_loop()
        loop.start_worker()
        wait_for(lambda: loop.receiver.snapshot()["active"])
        with self.assertRaises(OSError):
            DtlsConnection({"client_key": "ff" * 16, "hue_key": "loopback-test",
                            "bridge": "127.0.0.1"})
        self.assertEqual(loop.frames(), [])
        connection = loop.start()
        loop.frame(connection, 23456)
        wait_for(lambda: len(loop.frames()) == 1)
        self.assertEqual(loop.frames()[0]["colors"][0], [0, 23456, 0, 0])

    @unittest.skipUnless(shutil.which("openssl"), "OpenSSL required for loopback DTLS")
    def test_other_udp_peer_is_discarded_without_disrupting_authenticated_stream(self):
        guard, loop = self.guarded_loop()
        connection = loop.start()
        loop.frame(connection, 11111)
        wait_for(lambda: len(loop.frames()) == 1)
        before = guard.snapshot()["discarded_datagrams"]
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as other_peer:
            other_peer.sendto(b"synthetic-other-peer", guard.address)
        wait_for(lambda: guard.snapshot()["discarded_datagrams"] == before + 1)
        loop.frame(connection, 22222)
        wait_for(lambda: len(loop.frames()) == 2)
        self.assertEqual(loop.frames()[-1]["colors"][0], [0, 22222, 0, 0])

    @unittest.skipUnless(shutil.which("openssl"), "OpenSSL required for loopback DTLS")
    def test_shutdown_stops_active_and_queued_workers_before_closing_guard(self):
        guard, loop = self.guarded_loop()
        loop.receiver.bind(loop.group)
        connection = loop.start()
        loop.frame(connection)
        wait_for(lambda: len(loop.frames()) == 1)
        queued = loop.start_worker()
        loop.receiver.shutdown(timeout=4)
        queued.join(1)
        for worker in loop.threads:
            worker.join(1)
            self.assertFalse(worker.is_alive())
        self.assertTrue(all(process.poll() is not None for process in loop.servers))
        self.assertFalse(loop.group.stream["active"])
        self.assertFalse(guard.snapshot()["running"])
        count = len(loop.servers)
        late = loop.start_worker()
        late.join(1)
        self.assertFalse(late.is_alive())
        self.assertEqual(len(loop.servers), count)
        loop.receiver.shutdown(timeout=1)

    @unittest.skipUnless(shutil.which("openssl"), "OpenSSL required for loopback DTLS")
    def test_shutdown_timeout_retains_guard_until_superseded_spawn_finishes(self):
        guard, loop = self.guarded_loop()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def delayed_spawn():
            entered.set()
            release.wait(3)

        loop.before_spawn = delayed_spawn
        loop.start_worker()
        self.assertTrue(entered.wait(2))
        with self.assertRaises(TimeoutError):
            loop.receiver.shutdown(timeout=0.01)
        self.assertTrue(guard.snapshot()["running"])
        release.set()
        loop.receiver.shutdown(timeout=4)
        self.assertFalse(guard.snapshot()["running"])
        self.assertTrue(all(process.poll() is not None for process in loop.servers))

    def test_disabled_receiver_still_discards_idle_ingress_without_any_forwarding(self):
        guard, loop = self.guarded_loop()
        loop.receiver.enabled = False
        worker = loop.start_worker()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(loop.servers, [])
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            sender.sendto(b"synthetic-hold-packet", guard.address)
        wait_for(lambda: guard.snapshot()["discarded_datagrams"] == 1)
        self.assertEqual(loop.messages, [])
        self.assertFalse(loop.group.stream["active"])
        loop.receiver.shutdown(timeout=1)
        self.assertFalse(guard.snapshot()["running"])
