import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "experiments" / "hue-bridge"))
from entertainment_stream import VideoLease  # noqa: E402
from live_adapter import PilotController  # noqa: E402
from test_entertainment_stream import envelope  # noqa: E402
from test_live_adapter import LEFT, RIGHT, targets  # noqa: E402
from video_output import VideoOutput, govee_stream_state  # noqa: E402


class FakeRelay:
    def __init__(self):
        self.frames = []
        self.started = 0
        self.stopped = 0

    def start(self):
        self.started += 1
        return True

    def send(self, frame):
        self.frames.append(copy.deepcopy(frame))

    def stop(self):
        self.stopped += 1


class FakeProxy:
    def __init__(self):
        self.value = {"healthy": True, "native_on": True, "native_mode": "normal", "native_age": 0}

    def snapshot(self):
        return dict(self.value)

    def restore_scene(self):
        self.value["restore_requested"] = True


class VideoTests(unittest.TestCase):
    def setUp(self):
        self.now = 10.0
        self.relay = FakeRelay()
        self.sent = []
        self.controller = PilotController(
            targets(),
            sender=lambda device, cmd, data: self.sent.append((cmd, data)) or True,
            clock=lambda: self.now,
            wall_clock=lambda: 1000,
        )
        self.controller.update_statuses(
            {
                key: ({"onOff": 1, "brightness": 65, "color": {"r": 0, "g": 0, "b": 0}}, self.now)
                for key in (LEFT, RIGHT)
            }
        )
        self.lease = VideoLease(clock=lambda: self.now, wall_clock=lambda: 1000)
        self.proxies = [FakeProxy() for _ in range(4)]
        self.output = VideoOutput(self.controller, self.relay, self.proxies, lease=self.lease)

    def test_black_video_frame_is_color_black_without_power_command(self):
        self.lease.submit(envelope(colors=[[i, 0, 0, 0] for i in range(14)]))
        self.output.process_once()
        self.output.render()
        self.assertTrue(self.sent)
        self.assertTrue(all(name != "turn" for name, _ in self.sent))
        self.assertEqual(govee_stream_state((0, 0, 0))["rgb"], [0, 0, 0])

    def test_video_leases_expire_release_native_stream_and_clear_dedup(self):
        self.lease.submit(envelope())
        self.output.process_once()
        self.output.render()
        self.assertTrue(self.output.active())
        self.assertEqual(len(self.relay.frames), 1)
        self.now += 0.6
        self.output.process_once()
        self.assertFalse(self.output.active())
        self.assertEqual(self.relay.stopped, 1)

    def test_native_owner_off_or_foreign_stream_blocks_start(self):
        for field, value in (
            ("native_on", False),
            ("native_mode", "streaming"),
            ("healthy", False),
            ("native_age", 3),
        ):
            with self.subTest(field=field):
                self.proxies[0].value.update(
                    native_on=True, native_mode="normal", healthy=True, native_age=0
                )
                self.proxies[0].value[field] = value
                self.lease.submit(envelope(sequence=self.lease.sequence + 1))
                self.output.process_once()
                self.assertEqual(self.relay.started, 0)
                self.assertFalse(self.output.ready)

    def test_owner_off_during_our_stream_releases_it_without_turning_on(self):
        self.lease.submit(envelope())
        self.output.process_once()
        self.proxies[0].value["native_on"] = False
        self.output.process_once()
        self.assertFalse(self.output.ready)
        self.assertEqual(self.relay.stopped, 1)

    def test_handshake_delay_uses_latest_frame_instead_of_old_initial_frame(self):
        self.lease.submit(envelope())
        original = self.relay.start

        def start():
            self.lease.submit(envelope(sequence=2, colors=[[i, 0, 0, 0] for i in range(14)]))
            return original()

        self.relay.start = start
        self.output.process_once()
        self.assertTrue(all(rgb == (0, 0, 0) for rgb in self.relay.frames[0].values()))

    def test_short_video_explicitly_restores_scene_without_streaming_poll(self):
        self.lease.submit(envelope())
        self.output.process_once()
        self.lease.submit(envelope("stop", sequence=2))
        self.output.process_once()
        self.assertTrue(all(proxy.value.get("restore_requested") for proxy in self.proxies))

    def test_release_failure_is_retried_after_lease_ends(self):
        self.lease.submit(envelope())
        self.output.process_once()
        original = self.relay.stop
        self.relay.stop = lambda: (_ for _ in ()).throw(OSError("temporary outage"))
        self.lease.submit(envelope("stop", sequence=2))
        self.output.process_once()
        self.assertTrue(self.output.active())
        self.relay.stop = original
        self.output.process_once()
        self.assertFalse(self.output.active())
        self.assertEqual(self.relay.stopped, 1)

    def test_cleanup_retries_even_while_blocked_session_keeps_sending_frames(self):
        self.lease.submit(envelope())
        self.output.process_once()
        original = self.relay.stop
        self.relay.stop = lambda: (_ for _ in ()).throw(OSError("temporary outage"))
        self.proxies[0].value["native_on"] = False
        self.output.process_once()
        self.assertTrue(self.output.cleanup_pending)
        self.relay.stop = original
        self.lease.submit(envelope(sequence=2))
        self.output.process_once()
        self.assertFalse(self.output.cleanup_pending)
        self.assertEqual(self.relay.stopped, 1)
