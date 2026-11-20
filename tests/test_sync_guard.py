"""The Sync guard restarts Sync on the box when frames do not reach the adapter."""

import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge"))
from sync_guard import REPAIR_INTERVAL, RESTART_PAUSE, StreamIngress, SyncGuard, udp_port_free  # noqa: E402


class Box:
    """A Sync Box as SyncBox shows it; `drops` makes it give up Sync after a Start."""

    def __init__(self, syncing=True):
        self.syncing = syncing
        self.state = "ready"
        self.calls = []
        self.drops = False

    def status(self):
        return {"state": self.state, "syncing": self.syncing}

    def set_sync(self, active):
        self.calls.append(active)
        self.syncing = active and not self.drops
        return 200, self.status()


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class SyncGuardTests(unittest.TestCase):
    def setUp(self):
        self.box = Box()
        self.stream = "active"
        self.clock = Clock()
        self.guard = SyncGuard(self.box, lambda: self.stream, clock=self.clock,
                               sleep=self.clock.sleep)

    def run_for(self, seconds, step=5.0):
        restarts = 0
        end = self.clock.now + seconds
        while self.clock.now < end:
            self.clock.now += step
            restarts += self.guard.tick()
        return restarts

    def test_sync_with_frames_is_left_alone(self):
        self.assertEqual(self.run_for(300), 0)
        self.assertEqual(self.box.calls, [])
        self.assertEqual(self.guard.public(), {})

    def test_sync_without_frames_is_restarted_after_20_s(self):
        self.stream = "waiting"
        self.assertEqual(self.run_for(15), 0)
        self.assertEqual(self.run_for(10), 1)
        self.assertEqual(self.box.calls, [False, True], "Stop, then Start")
        self.assertEqual(self.guard.public(), {"recovery": "restarting"})
        self.stream = "active"
        self.run_for(5)
        self.assertEqual(self.guard.public(), {}, "frames are back")

    def test_restarts_are_spaced_and_end_after_three(self):
        self.stream = "off"
        times = []
        end = self.clock.now + 600
        while self.clock.now < end:
            self.clock.now += 5
            if self.guard.tick():
                times.append(self.clock.now)
        self.assertEqual(len(times), 3)
        gaps = [b - a for a, b in zip(times, times[1:])]
        self.assertGreaterEqual(gaps[0], 50 - RESTART_PAUSE)
        self.assertGreater(gaps[1], gaps[0], "the pauses grow")
        self.assertEqual(self.guard.public(), {"recovery": "failed"})

    def test_a_box_that_drops_sync_right_after_a_start_is_started_again(self):
        self.box.syncing = False
        self.guard.started()
        self.box.drops = True
        self.assertEqual(self.run_for(25), 1, "the dropped Start is retried")
        self.assertFalse(self.box.syncing, "the bridge is still warming up")
        self.box.drops = False
        self.stream = "active"
        self.assertEqual(self.run_for(60), 1, "the next try after a pause")
        self.assertTrue(self.box.syncing)
        self.run_for(5)
        self.assertEqual(self.guard.public(), {})

    def test_a_stop_is_never_undone(self):
        self.guard.started()
        self.run_for(40)
        self.box.syncing = False  # stopped in the Hue app long after the Start
        self.assertEqual(self.run_for(120), 0)
        self.guard.started()
        self.guard.stopped()
        self.box.syncing = False  # the owner's Stop from Light Studio
        self.assertEqual(self.run_for(60), 0)
        self.assertEqual(self.box.calls, [])

    def test_sync_started_elsewhere_and_stopped_is_not_restarted(self):
        self.run_for(10)
        self.box.syncing = False
        self.assertEqual(self.run_for(60), 0)

    def test_nothing_is_done_when_the_adapter_is_down_or_another_app_streams(self):
        for stream in ("unknown", "foreign", "paused", None):
            self.stream = stream
            self.assertEqual(self.run_for(200), 0, stream)
        self.assertEqual(self.box.calls, [])

    def test_nothing_is_done_while_the_box_is_offline_or_switching(self):
        self.stream = "waiting"
        self.box.state = "offline"
        self.assertEqual(self.run_for(200), 0)
        self.box.state = "ready"
        self.box.status = lambda: {"state": "ready", "syncing": True, "changing": False}
        self.assertEqual(self.run_for(200), 0)

    def test_the_owners_start_resets_a_failed_recovery(self):
        self.stream = "off"
        self.run_for(600)
        self.assertEqual(self.guard.public(), {"recovery": "failed"})
        self.guard.started()
        self.assertEqual(self.guard.public(), {})

    def bridge_guard(self):
        self.frames = 5000
        guard = SyncGuard(self.box, lambda: self.stream, lambda: self.frames,
                          clock=self.clock, sleep=self.clock.sleep)
        self.guard = guard
        self.run_for(30)
        return guard

    def restart_bridge(self):
        """The box stops Sync as the bridge goes down; the bridge returns counting from 0."""
        self.box.syncing = False
        self.stream = "off"
        self.frames = None
        restarts = self.run_for(5)
        self.frames = 0
        return restarts

    def test_sync_stopped_by_a_bridge_restart_is_started_again(self):
        self.bridge_guard()
        self.assertEqual(self.restart_bridge() + self.run_for(10), 1)
        self.assertEqual(self.box.calls, [False, True])
        self.assertTrue(self.box.syncing)

    def test_a_bridge_restart_seen_only_by_its_counter_counts_too(self):
        self.bridge_guard()
        self.box.syncing = False
        self.stream = "off"
        self.run_for(10)
        self.frames = 3
        self.assertEqual(self.run_for(10), 1)

    def test_a_stop_while_the_bridge_runs_is_kept_even_if_it_restarts_later(self):
        self.bridge_guard()
        self.box.syncing = False  # stopped in the Hue app
        self.run_for(60)
        self.restart_bridge()
        self.assertEqual(self.run_for(120), 0)
        self.assertEqual(self.box.calls, [])

    def test_a_bridge_restart_while_sync_is_off_starts_nothing(self):
        self.box.syncing = False
        self.bridge_guard()
        self.restart_bridge()
        self.assertEqual(self.run_for(120), 0)

    def ingress(self, published=False):
        """A host whose stream port is lost until the container restarts."""
        state = {"published": published, "runs": []}

        def run(command, **kwargs):
            state["runs"].append((command, self.box.syncing))
            state["published"] = True

        ingress = StreamIngress("bridge", lambda: True, port_free=lambda port:
                                not state["published"], run=run, docker=lambda: "docker",
                                clock=self.clock, sleep=self.clock.sleep)
        return ingress, state

    def test_a_lost_stream_port_is_published_again_while_the_box_is_quiet(self):
        ingress, state = self.ingress()
        self.guard.ingress = ingress
        self.stream = "waiting"
        self.assertEqual(self.run_for(25), 1)
        self.assertEqual(state["runs"], [(["docker", "restart", "bridge"], False)])
        self.assertTrue(self.box.syncing, "Sync starts again after the port is back")

    def test_a_published_port_restarts_only_sync(self):
        ingress, state = self.ingress(published=True)
        self.guard.ingress = ingress
        self.stream = "waiting"
        self.assertEqual(self.run_for(25), 1)
        self.assertEqual(state["runs"], [])

    def test_the_container_is_restarted_at_most_once_per_interval(self):
        ingress, state = self.ingress()
        self.assertTrue(ingress.repair())
        state["published"] = False
        self.assertFalse(ingress.repair())
        self.clock.now += REPAIR_INTERVAL
        self.assertTrue(ingress.repair())
        self.assertEqual(len(state["runs"]), 2)

    def test_no_docker_or_a_bridge_that_does_not_answer_repairs_nothing(self):
        ingress, state = self.ingress()
        ingress.docker = lambda: None
        self.assertFalse(ingress.repair())
        ingress.bridge_up = lambda: False
        self.assertFalse(ingress.lost(), "a stopped bridge is not a lost port")
        self.assertEqual(state["runs"], [])

    def test_udp_port_free_sees_a_bound_port(self):
        import socket
        holder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        holder.bind(("127.0.0.1", 0))
        try:
            self.assertFalse(udp_port_free(holder.getsockname()[1], "127.0.0.1"))
        finally:
            holder.close()

    def test_run_survives_a_failing_check_and_stops(self):
        calls = []

        def broken():
            calls.append(1)
            raise OSError("box gone")

        self.box.status = broken
        stop = threading.Event()
        guard = SyncGuard(self.box, lambda: "waiting")
        import sync_guard
        original = sync_guard.TICK_SECONDS
        sync_guard.TICK_SECONDS = 0.01
        try:
            worker = threading.Thread(target=guard.run, args=(stop,))
            worker.start()
            while len(calls) < 3:
                threading.Event().wait(0.01)
            stop.set()
            worker.join(1)
        finally:
            sync_guard.TICK_SECONDS = original
        self.assertFalse(worker.is_alive())


if __name__ == "__main__":
    unittest.main()
