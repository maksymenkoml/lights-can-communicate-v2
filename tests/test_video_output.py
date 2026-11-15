import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "experiments" / "hue-bridge"))
from entertainment_stream import VideoLease  # noqa: E402
from live_adapter import PilotController, normalize_state  # noqa: E402
from power_owner import PowerOwner  # noqa: E402
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
    def test_power_owner_pauses_govee_only_and_preserves_native_stream(self):
        gate = PowerOwner(targets(), clock=lambda: self.now, wall_clock=lambda: 1000)
        sent = []
        self.controller = PilotController(
            targets(), sender=gate.wrap(
                lambda device, command, _data: sent.append((device['device'], command)) or True),
            clock=lambda: self.now, wall_clock=lambda: 1000, power_owner=gate)
        self.controller.update_statuses({key: ({'onOff': 1, 'brightness': 65}, self.now)
                                         for key in (LEFT, RIGHT)})
        self.output.controller = self.controller
        self.lease.submit(envelope())
        self.output.process_once()
        self.output.render()
        self.assertEqual(sent, [])
        self.assertEqual(len(self.relay.frames), 1)
        gate.submit({'session': 'home', 'sequence': 1, 'issued_at': 1000,
                     'states': {'left': True, 'right': True}})
        self.lease.submit(envelope(sequence=2))
        self.output.process_once()
        self.output.render()
        self.assertEqual({device for device, _ in sent}, {LEFT, RIGHT})
        gate.submit({'session': 'home', 'sequence': 2, 'issued_at': 1000,
                     'states': {'right': False}})
        sent.clear()
        self.lease.submit(envelope(sequence=3, colors=[[i, 0, 0, 0] for i in range(14)]))
        self.output.process_once()
        self.output.render()
        self.assertEqual({device for device, _ in sent}, {LEFT})
        self.assertEqual(len(self.relay.frames), 3)
        self.assertFalse(any(command == 'turn' for _, command in sent))

    def test_gradient_stream_routes_three_anchors_per_govee_and_keeps_native_ids(self):
        from test_govee_gradient import gradient_stream

        stream = gradient_stream()
        self.output.lease = stream.VideoLease(clock=lambda: self.now, wall_clock=lambda: 1000)
        for device in self.controller.targets:
            device["segment_count"] = 7
        colors = {i: (0, 0, 0) for i in range(18)}
        colors.update(
            {
                0: (65535, 0, 0),
                14: (0, 65535, 0),
                15: (0, 0, 65535),
                1: (0, 0, 65535),
                16: (65535, 0, 0),
                17: (0, 65535, 0),
            }
        )
        message = envelope(colors=[[k, *v] for k, v in colors.items()])
        with patch("video_output.GOVEE_CHANNELS", stream.GOVEE_CHANNELS):
            self.assertTrue(self.output.submit(message))
            self.assertTrue(self.output.process_once())
            self.output.render()
        left, right = self.controller.target("left"), self.controller.target("right")
        self.assertEqual(left["segments"][0], (255, 0, 0))
        self.assertEqual(left["segments"][3], (0, 255, 0))
        self.assertEqual(left["segments"][6], (0, 0, 255))
        self.assertEqual(right["segments"][0], (0, 0, 255))
        self.assertEqual(right["segments"][6], (0, 255, 0))
        self.assertEqual(left["brightness"], 65)
        self.assertEqual([cmd for cmd, _ in self.sent].count("ptReal"), 2)
        self.assertTrue(all(cmd != "turn" for cmd, _ in self.sent))

    def test_video_intensity_uses_rgb_with_fixed_calibrated_global_brightness(self):
        self.assertEqual(
            normalize_state(govee_stream_state((32768, 0, 65535)), 0.65),
            {"on": True, "brightness": 65, "rgb": (128, 0, 255)},
        )
        self.assertEqual(
            normalize_state(govee_stream_state((16384, 0, 32768)), 0.65),
            {"on": True, "brightness": 65, "rgb": (64, 0, 128)},
        )

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
        self.output = VideoOutput(
            self.controller, self.relay, self.proxies, lease=self.lease, clock=lambda: self.now
        )

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

    def test_source_before_lights_on_waits_then_resumes_without_source_restart(self):
        for proxy in self.proxies:
            proxy.value["native_on"] = False
        self.lease.submit(envelope())
        self.assertFalse(self.output.process_once())
        self.output.render()
        self.assertEqual(self.relay.started, 0)
        self.assertEqual(self.sent, [])

        for proxy in self.proxies[:3]:
            proxy.value["native_on"] = True
        self.now += 0.1
        self.lease.submit(envelope(sequence=2))
        self.assertFalse(self.output.process_once())
        self.assertEqual(self.relay.started, 0)

        self.proxies[3].value["native_on"] = True
        self.now += 0.1
        self.lease.submit(envelope(sequence=3))
        self.assertTrue(self.output.process_once())
        self.output.render()
        self.assertTrue(self.output.ready)
        self.assertEqual(self.relay.started, 1)
        self.assertEqual(len(self.relay.frames), 1)
        self.assertTrue(self.sent)
        self.assertTrue(all(command != "turn" for command, _ in self.sent))

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

    def test_handshake_stale_cache_waits_for_fresh_native_poll_without_blocking_session(self):
        original = self.relay.start

        def start():
            for proxy in self.proxies:
                proxy.value["native_age"] = 2.1
            return original()

        self.relay.start = start
        self.lease.submit(envelope())
        self.output.process_once()
        self.assertEqual(self.relay.frames, [])
        self.assertIsNone(self.output.blocked_session)
        self.assertEqual(self.relay.stopped, 0)
        for proxy in self.proxies:
            proxy.value["native_age"] = 0
        self.assertTrue(self.output.process_once())
        self.assertEqual(len(self.relay.frames), 1)
        self.assertEqual(self.relay.started, 1)

    def test_temporary_stale_native_cache_retries_after_releasing_stream(self):
        self.lease.submit(envelope())
        self.output.process_once()
        self.now += 3
        self.lease.submit(envelope(sequence=2))
        self.proxies[0].value["native_age"] = 3
        self.output.process_once()
        self.assertFalse(self.output.ready)
        self.assertIsNone(self.output.blocked_session)
        self.now += 2
        self.proxies[0].value["native_age"] = 0
        self.lease.submit(envelope(sequence=3))
        self.assertTrue(self.output.process_once())
        self.assertEqual(self.relay.started, 2)

    def test_source_expiry_during_handshake_releases_without_any_frame(self):
        original = self.relay.start

        def slow_start():
            self.now += 1
            return original()

        self.relay.start = slow_start
        self.lease.submit(envelope())
        self.assertFalse(self.output.process_once())
        self.output.render()
        self.assertEqual(self.relay.frames, [])
        self.assertEqual(self.sent, [])
        self.assertEqual(self.relay.stopped, 1)
        self.assertFalse(self.output.ready)

    def test_owner_off_during_handshake_blocks_same_source_after_owner_turns_on(self):
        original = self.relay.start

        def start_with_owner_off():
            self.proxies[0].value["native_on"] = False
            return original()

        self.relay.start = start_with_owner_off
        self.lease.submit(envelope())
        self.assertFalse(self.output.process_once())
        self.assertEqual(self.output.blocked_session, "test-session")
        self.proxies[0].value["native_on"] = True
        self.lease.submit(envelope(sequence=2))
        self.assertFalse(self.output.process_once())
        self.assertEqual(self.relay.frames, [])

    def test_one_govee_off_does_not_stop_other_govee_or_send_power_commands(self):
        sent = []
        self.controller.sender = lambda device, cmd, data: sent.append(
            (device["device"], cmd, data)
        ) or True
        self.controller.update_statuses({RIGHT: ({"onOff": 0}, self.now)})
        self.lease.submit(envelope())
        self.assertTrue(self.output.process_once())
        self.output.render()
        self.assertTrue(sent)
        self.assertEqual({device for device, _, _ in sent}, {LEFT})
        self.assertFalse(any(command == "turn" for _, command, _ in sent))

    def test_native_write_and_cleanup_failure_fences_scenes_until_retry_succeeds(self):
        self.lease.submit(envelope())
        self.output.process_once()
        original_stop = self.relay.stop
        self.relay.send = lambda _: (_ for _ in ()).throw(OSError("pipe closed"))
        self.relay.stop = lambda: (_ for _ in ()).throw(OSError("bridge unavailable"))
        self.assertFalse(self.output.process_once())
        self.assertTrue(self.output.cleanup_pending)
        self.assertTrue(self.output.active())
        self.assertFalse(any(proxy.value.get("restore_requested") for proxy in self.proxies))
        self.lease.submit(envelope("stop", sequence=2))
        self.relay.stop = original_stop
        self.output.process_once()
        self.assertFalse(self.output.active())
        self.assertTrue(all(proxy.value.get("restore_requested") for proxy in self.proxies))
