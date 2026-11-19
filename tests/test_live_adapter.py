import copy
import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge"))
import live_adapter as adapter  # noqa: E402

LEFT = "AA:AA:AA:AA:AA:AA:AA:AA"
RIGHT = "BB:BB:BB:BB:BB:BB:BB:BB"


def targets():
    return [
        {"side": "left", "device": LEFT, "ip": "192.168.10.50", "brightness_scale": 0.65},
        {"side": "right", "device": RIGHT, "ip": "192.168.10.51", "brightness_scale": 0.65},
    ]


def command(sequence=1, xy=(0.7, 0.299), **values):
    return {
        "side": "left",
        "session": "probe-session",
        "sequence": sequence,
        "issued_at": 1000.0,
        "transitiontime": 20,
        "state": {"on": True, "bri": 254, "colormode": "xy", "xy": list(xy)},
        **values,
    }


class LiveAdapterTests(unittest.TestCase):
    def test_http_routes_each_confirmed_hue_play_role_and_rejects_unknown_role(self):
        from hue_strip_proxy import HueStripProxy
        from test_hue_strip_proxy import FakeHue
        from test_native_profile import PLAY_IDS

        proxies = {}
        for role, resource in PLAY_IDS.items():
            client = FakeHue()
            client.resource_id = resource
            client.native["id"] = resource
            proxies[role] = HueStripProxy(client, wall_clock=lambda: 1000, role=role)
        from entertainment_stream import VideoLease
        from test_entertainment_stream import envelope

        lease = VideoLease(wall_clock=lambda: 1000)
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            adapter.handler_for(
                self.controller, "test", hue_play_proxies=proxies, video_output=lease
            ),
        )
        worker = threading.Thread(target=server.serve_forever)
        worker.start()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            url = "http://127.0.0.1:" + str(server.server_port)
            request = urllib.request.Request(
                url + "/stream",
                data=json.dumps(envelope()).encode(),
                headers={"X-Adapter-Token": "test"},
                method="POST",
            )
            with opener.open(request) as response:
                self.assertTrue(json.load(response)["accepted"])
            self.assertTrue(lease.active())
            for role in PLAY_IDS:
                request = urllib.request.Request(
                    url + "/command",
                    data=json.dumps(command(side=role)).encode(),
                    headers={"X-Adapter-Token": "test"},
                    method="POST",
                )
                with opener.open(request) as response:
                    self.assertTrue(json.load(response)["accepted"])
                self.assertIsNotNone(proxies[role].desired)
            request = urllib.request.Request(
                url + "/command",
                data=json.dumps(command(side="other_hue")).encode(),
                headers={"X-Adapter-Token": "test"},
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as rejected:
                opener.open(request)
            self.assertEqual(rejected.exception.code, 400)
            request = urllib.request.Request(url + "/status", headers={"X-Adapter-Token": "test"})
            with opener.open(request) as response:
                self.assertEqual(set(json.load(response)["hue_play"]), set(PLAY_IDS))
        finally:
            server.shutdown()
            server.server_close()
            worker.join()

    def test_owner_hold_prevents_adapter_start_before_any_network_setup(self):
        with TemporaryDirectory(prefix="tmp_adapter_hold_") as directory:
            folder = Path(directory)
            (folder / "live-output-hold").write_text("Owner is sleeping")
            with (
                patch(
                    "sys.argv",
                    [
                        "live_adapter.py",
                        "--targets",
                        str(folder / "missing-targets.json"),
                        "--token-file",
                        str(folder / "token"),
                    ],
                ),
                self.assertRaisesRegex(SystemExit, "hold"),
            ):
                adapter.main()

    def setUp(self):
        self.now = 0.0
        self.sent = []
        self.controller = adapter.GoveeController(
            targets(), sender=self.send, clock=lambda: self.now, wall_clock=lambda: 1000.0
        )
        self.controller.update_statuses(
            {
                LEFT: (
                    {
                        "onOff": 1,
                        "brightness": 65,
                        "color": {"r": 255, "g": 0, "b": 0},
                        "colorTemInKelvin": 0,
                    },
                    0.0,
                ),
                RIGHT: (
                    {
                        "onOff": 1,
                        "brightness": 65,
                        "color": {"r": 0, "g": 0, "b": 255},
                        "colorTemInKelvin": 0,
                    },
                    0.0,
                ),
            }
        )

    def send(self, device, name, data):
        self.sent.append((device["device"], name, copy.deepcopy(data)))
        return True

    def test_brightness_mapping_keeps_calibration_and_color(self):
        target = adapter.normalize_state(command()["state"], 0.65)
        self.assertEqual(target["brightness"], 65)
        self.assertEqual(target["rgb"], (255, 0, 0))

    def test_gradient_requires_explicit_segment_capability_and_keeps_calibration(self):
        state = {
            "on": True,
            "bri": 254,
            "colormode": "rgb",
            "segments": [[120, 0, 0], [0, 120, 0], [0, 0, 120]],
        }
        with self.assertRaises(ValueError):
            adapter.normalize_state(state, 0.65)
        target = adapter.normalize_state(state, 0.65, segment_count=7)
        self.assertEqual(target["brightness"], 65)
        self.assertEqual(len(target["segments"]), 7)
        self.assertEqual(target["segments"][0], (120, 0, 0))
        self.assertEqual(target["segments"][3], (0, 120, 0))
        self.assertEqual(target["segments"][6], (0, 0, 120))

    def test_gradient_transitions_interpolate_every_zone(self):
        first = {"on": True, "brightness": 65, "rgb": (120, 0, 0)}
        last = {"on": True, "brightness": 65, "segments": [(0, 0, 120)] * 7}
        middle = adapter.blend(first, last, 0.5)
        self.assertEqual(middle["segments"], [(60, 0, 60)] * 7)
        self.assertEqual(adapter.blend(first, last, 1), last)

    def test_scene_gradient_validates_points_and_respects_explicit_single_color(self):
        state = {
            "on": True,
            "bri": 127,
            "colormode": "xy",
            "_color_kind": "gradient",
            "gradient": {
                "points": [
                    {"color": {"xy": {"x": 0.7, "y": 0.299}}},
                    {"color": {"xy": {"x": 0.15, "y": 0.06}}},
                ]
            },
        }
        target = adapter.normalize_state(state, 0.65, segment_count=7)
        self.assertEqual(target["brightness"], 32)
        self.assertEqual(target["segments"][0], (255, 0, 0))
        self.assertEqual(target["segments"][-1], (0, 0, 255))
        for points in (None, [], [None], [True], [{"color": {"xy": {"x": 1, "y": 1}}}]):
            with self.subTest(points=points), self.assertRaises(ValueError):
                adapter.normalize_state({**state, "gradient": {"points": points}}, 0.65, 7)
        single = adapter.normalize_state(
            {**state, "_color_kind": "xy", "xy": [0.7, 0.299]}, 0.65, segment_count=7
        )
        self.assertEqual(single["rgb"], (255, 0, 0))
        self.assertNotIn("segments", single)

    def test_gradient_batches_zones_and_deduplicates_without_power_commands(self):
        selected = targets()
        selected[0]["segment_count"] = 7
        selected[0]["max_commands_per_second"] = 10
        controller = adapter.GoveeController(
            selected,
            sender=self.send,
            clock=lambda: self.now,
            wall_clock=lambda: 1000,
        )
        controller.update_statuses(self.controller.statuses)
        state = {
            "on": True,
            "bri": 254,
            "colormode": "rgb",
            "segments": [[120, 0, 0], [0, 120, 0], [0, 0, 120]],
        }
        controller.submit(command(state=state, transitiontime=0))
        controller.render()
        self.assertEqual([name for _, name, _ in self.sent], ["ptReal", "brightness"])
        self.assertEqual(len(self.sent[0][2]["command"]), 7)
        self.sent.clear()
        controller.render()
        self.assertEqual(self.sent, [])

        controller.submit(
            command(sequence=2, state={**state, "segments": [[0, 0, 120]]}, transitiontime=0)
        )
        self.now = 0.1
        controller.render()
        self.assertEqual(self.sent, [])
        self.now = 0.2
        controller.render()
        self.assertEqual([name for _, name, _ in self.sent], ["ptReal"])
        self.sent.clear()
        controller.update_statuses({LEFT: ({"onOff": 0}, self.now)})
        controller.render()
        self.assertEqual(self.sent, [])

    def test_gradient_packet_budget_is_separate_from_legacy_color_budget(self):
        selected = targets()
        selected[0].update(
            segment_count=7, max_commands_per_second=10, max_gradient_packets_per_second=2
        )
        controller = adapter.GoveeController(
            selected, sender=self.send, clock=lambda: self.now, wall_clock=lambda: 1000
        )
        controller.update_statuses(self.controller.statuses)
        state = {"on": True, "bri": 254, "colormode": "rgb", "segments": [[120, 0, 0]]}
        controller.submit(command(state=state, transitiontime=0))
        controller.render()
        self.sent.clear()
        controller.submit(
            command(sequence=2, state={**state, "segments": [[0, 0, 120]]}, transitiontime=0)
        )
        self.now = 0.2
        controller.render()
        self.assertEqual(self.sent, [])
        self.now = 1.0
        controller.update_statuses({LEFT: ({"onOff": 1}, self.now)})
        controller.render()
        self.assertEqual([name for _, name, _ in self.sent], ["ptReal"])

        self.sent.clear()
        self.now = 0
        legacy = adapter.GoveeController(
            selected, sender=self.send, clock=lambda: self.now, wall_clock=lambda: 1000
        )
        legacy.update_statuses(self.controller.statuses)
        legacy.submit(command(transitiontime=0))
        legacy.render()
        self.sent.clear()
        legacy.submit(command(sequence=2, xy=(0.15, 0.06), transitiontime=0))
        self.now = 0.2
        legacy.render()
        self.assertEqual([name for _, name, _ in self.sent], ["colorwc"])

    def test_streaming_uses_dreamview_at_the_stream_rate(self):
        from govee_segments import razer_colors

        selected = targets()
        selected[0].update(segment_count=7, max_gradient_packets_per_second=2)
        controller = adapter.GoveeController(
            selected, sender=self.send, clock=lambda: self.now, wall_clock=lambda: 1000
        )
        controller.update_statuses(self.controller.statuses)
        controller.set_streaming(True)
        state = {"on": True, "bri": 254, "colormode": "rgb", "segments": [[120, 0, 0]]}
        controller.submit(command(state=state, transitiontime=0))
        controller.render()
        frames = [razer_colors(data) for _, name, data in self.sent if name == "razer"]
        self.assertEqual(frames[0], None)
        self.assertEqual(frames[1], [[120, 0, 0]] * 7)
        self.assertNotIn("ptReal", [name for _, name, _ in self.sent])
        # 30 frames/s, not the 2/s ptReal budget: after the first burst (mode,
        # frame and brightness) the next frame goes 0.1 s later.
        self.assertEqual(adapter.STREAM_FRAMES_PER_SECOND, 30)
        for sequence, (now, red) in enumerate(((0.12, 60), (0.24, 30)), start=2):
            self.sent.clear()
            self.now = now
            controller.update_statuses({LEFT: ({"onOff": 1}, self.now)})
            controller.submit(command(sequence=sequence, transitiontime=0,
                                      state={**state, "segments": [[red, 0, 0]]}))
            controller.render()
            self.assertEqual([razer_colors(data) for _, name, data in self.sent
                              if name == "razer"], [[[red, 0, 0]] * 7])
        # Video output clears last_sent every tick; an unchanged brightness is not
        # re-sent, so every slot carries a color frame.
        self.sent.clear()
        self.now = 0.36
        controller.update_statuses({LEFT: ({"onOff": 1}, self.now)})
        controller.last_sent.clear()
        controller.render()
        self.assertEqual([name for _, name, _ in self.sent], ["razer"])
        # Render ticks every 30 ms are late for most 33 ms slots; the cadence still
        # holds 30 frames/s instead of falling to one frame per 60 ms.
        sent = 0
        for tick in range(1, 51):
            self.now = 0.36 + tick * 0.03
            controller.update_statuses({LEFT: ({"onOff": 1}, self.now)})
            controller.last_sent.clear()
            before = len(self.sent)
            controller.render()
            sent += len(self.sent) > before
        self.assertGreaterEqual(sent, 44)
        self.assertLessEqual(sent, 46)
        # While frames flow the mode is not sent again: re-sending it blinks the lamp.
        self.sent.clear()
        self.now = 11
        controller.update_statuses({LEFT: ({"onOff": 1}, self.now)})
        controller.submit(command(sequence=9, transitiontime=0, state=state))
        controller.render()
        self.assertNotIn(None, [razer_colors(data) for _, name, data in self.sent
                                if name == "razer"])
        # Back to scene output: DreamView off first, then ptReal at the scene budget.
        self.sent.clear()
        self.now = 12
        controller.set_streaming(False)
        controller.update_statuses({LEFT: ({"onOff": 1}, self.now)})
        controller.render()
        self.assertEqual([name for _, name, _ in self.sent][:2], ["razer", "ptReal"])
        self.assertEqual(controller.snapshot()["dreamview_sides"], [])

    def test_dreamview_is_switched_on_only_when_the_lamp_may_have_left_it(self):
        from govee_segments import razer_colors

        selected = targets()
        selected[0].update(segment_count=7)
        controller = adapter.GoveeController(
            selected, sender=self.send, clock=lambda: self.now, wall_clock=lambda: 1000
        )
        controller.set_streaming(True)
        state = {"on": True, "bri": 254, "colormode": "rgb", "segments": [[120, 0, 0]]}
        sequence = iter(range(1, 10000))

        def tick(at, status=None, red=None):
            self.now = at
            controller.update_statuses({LEFT: (status or {"onOff": 1}, self.now)})
            if red is not None:
                controller.submit(command(sequence=next(sequence), transitiontime=0,
                                          state={**state, "segments": [[red, 0, 0]]}))
            before = len(self.sent)
            controller.render()
            return [razer_colors(data) for device, name, data in self.sent[before:]
                    if device == LEFT and name == "razer"]

        def modes(frames):
            return frames.count(None)

        # Stream start: the mode, then the current frame in the same burst.
        frames = tick(0, red=120)
        self.assertEqual(frames[:2], [None, [[120, 0, 0]] * 7])
        # A minute of changing frames, with a status miss every 10 s like the
        # slower H6076, never switches the mode on again.
        sent = 0
        for step in range(1, 1500):
            at = step * 0.04
            missing = step % 250 in range(1, 9)
            status = ({"onOff": 0, "identity_verified": False} if missing else None)
            frames = tick(at, status=status, red=step % 200 + 20)
            sent += modes(frames)
        self.assertEqual(sent, 0)
        # A still picture: the 30 s refresh frame keeps the lamp in DreamView.
        quiet = [modes(tick(60 + step * 0.5)) for step in range(1, 120)]
        self.assertEqual(sum(quiet), 0)
        # A verified Off ends DreamView on the lamp: the mode returns with a frame.
        tick(121, status={"onOff": 0, "identity_verified": True})
        frames = tick(122, red=90)
        self.assertEqual(frames[:2], [None, [[90, 0, 0]] * 7])
        # A status loss long enough for a power cycle does the same.
        for step in range(0, 7):
            tick(123 + step, status={"onOff": 0, "identity_verified": False})
        frames = tick(130.5, red=60)
        self.assertEqual(frames[:2], [None, [[60, 0, 0]] * 7])
        # A frame gap close to the lamp's one-minute timeout: the mode again.
        controller.update_statuses({LEFT: ({"onOff": 1}, 131)})
        with patch.object(controller, "effective_target", return_value=None):
            for step in range(1, 47):
                tick(130.5 + step)
        frames = tick(177, red=30)
        self.assertEqual(frames[:2], [None, [[30, 0, 0]] * 7])

    def test_stream_start_ignores_a_scene_slot_and_scene_waits_for_dreamview_off(self):
        from govee_segments import razer_colors

        selected = targets()
        selected[0].update(segment_count=7, max_gradient_packets_per_second=2)
        results = []

        def send(device, name, data):
            self.sent.append((device["side"], name, data))
            return results.pop(0) if results else True

        controller = adapter.GoveeController(
            selected, sender=send, clock=lambda: self.now, wall_clock=lambda: 1000
        )
        controller.update_statuses(self.controller.statuses)
        state = {"on": True, "bri": 254, "colormode": "rgb", "segments": [[120, 0, 0]]}
        controller.submit(command(state=state, transitiontime=0))
        controller.render()
        self.assertEqual(self.sent[0][1], "ptReal")
        # The scene slot lies 1 s ahead at 2 datagrams/s; the stream starts at once.
        self.sent.clear()
        self.now = 0.1
        controller.update_statuses({LEFT: ({"onOff": 1}, self.now)})
        controller.set_streaming(True)
        controller.submit(command(sequence=2, transitiontime=0,
                                  state={**state, "segments": [[0, 0, 90]]}))
        controller.render()
        self.assertIn([[0, 0, 90]] * 7, [razer_colors(data) for side, name, data in self.sent
                                         if side == "left" and name == "razer"])
        # Leaving DreamView fails: no scene color is sent or recorded as sent.
        self.sent.clear()
        self.now = 1.5
        controller.update_statuses({LEFT: ({"onOff": 1}, self.now)})
        controller.set_streaming(False)
        controller.submit(command(sequence=3, transitiontime=0, state=state))
        results.append(False)
        controller.render()
        self.assertEqual([name for side, name, _ in self.sent if side == "left"], ["razer"])
        self.assertNotIn("left", controller.last_sent)
        self.assertEqual(controller.snapshot()["dreamview_sides"], ["left"])
        # The next render leaves DreamView and then sends the pending scene.
        self.sent.clear()
        self.now = 1.6
        controller.update_statuses({LEFT: ({"onOff": 1}, self.now)})
        controller.render()
        self.assertEqual([name for side, name, _ in self.sent if side == "left"][:2],
                         ["razer", "ptReal"])
        self.assertEqual(controller.snapshot()["dreamview_sides"], [])

    def test_dreamview_frames_continue_through_a_short_status_miss(self):
        from govee_segments import razer_colors

        selected = targets()
        selected[0].update(segment_count=7)
        controller = adapter.GoveeController(
            selected, sender=self.send, clock=lambda: self.now, wall_clock=lambda: 1000
        )
        state = {"on": True, "bri": 254, "colormode": "rgb", "segments": [[120, 0, 0]]}
        sequence = iter(range(1, 100))

        def tick(at, status, red, bri=254):
            self.now = at
            if status is not None:
                controller.update_statuses({LEFT: (status, at)})
            controller.submit(command(sequence=next(sequence), transitiontime=0,
                                      state={**state, "bri": bri, "segments": [[red, 0, 0]]}))
            self.sent.clear()
            controller.render()
            return [(name, razer_colors(data) if name == "razer" else data)
                    for device, name, data in self.sent if device == LEFT]

        controller.set_streaming(True)
        self.assertEqual([name for name, _ in tick(0, {"onOff": 1}, 120)],
                         ["razer", "razer", "brightness"])
        # Status replies go missing for 0.9 s: frames keep flowing, brightness waits.
        missing = {"onOff": 0, "identity_verified": False}
        self.assertEqual(tick(0.5, missing, 90, bri=128), [("razer", [[90, 0, 0]] * 7)])
        self.assertEqual(tick(1.4, missing, 60, bri=128), [("razer", [[60, 0, 0]] * 7)])
        self.assertEqual(controller.skipped_state["left"], 0)
        # The status returns: the held brightness change goes out.
        self.assertIn("brightness", [name for name, _ in tick(1.5, {"onOff": 1}, 50, bri=128)])
        # A miss longer than the hold stops output.
        tick(2.0, missing, 40)
        self.assertEqual(tick(3.01, missing, 30), [])
        self.assertGreater(controller.skipped_state["left"], 0)
        # A verified Off stops frames at once.
        tick(3.5, {"onOff": 1}, 20)
        self.assertEqual(tick(3.7, {"onOff": 0, "identity_verified": True}, 10), [])
        # Scene output never rides out a miss.
        controller.set_streaming(False)
        tick(4.0, {"onOff": 1}, 10)
        self.assertEqual(tick(4.5, missing, 5), [])

    def test_snapshot_keeps_the_last_verified_reading_through_a_status_miss(self):
        def row():
            return next(item for item in self.controller.snapshot()["targets"]
                        if item["side"] == "left")

        self.now = 0.1
        self.assertEqual(row()["verified_native"]["onOff"], 1)
        self.assertAlmostEqual(row()["verified_age"], 0.1)
        unseen = adapter.GoveeController(targets(), sender=self.send, clock=lambda: self.now)
        unseen.update_statuses({RIGHT: ({"onOff": 0, "identity_verified": False}, self.now)})
        for item in unseen.snapshot()["targets"]:
            self.assertIsNone(item["verified_native"])
            self.assertIsNone(item["verified_age"])
        # A missed reply publishes an unverified placeholder; the verified reading stays.
        self.now = 0.9
        self.controller.update_statuses(
            {LEFT: ({"onOff": 0, "identity_verified": False}, self.now)})
        self.assertIs(row()["native"]["identity_verified"], False)
        self.assertEqual(row()["verified_native"]["onOff"], 1)
        self.assertAlmostEqual(row()["verified_age"], 0.9)
        # A verified Off replaces it at once.
        self.now = 1.0
        self.controller.update_statuses(
            {LEFT: ({"onOff": 0, "identity_verified": True}, self.now)})
        self.assertEqual(row()["verified_native"]["onOff"], 0)
        self.assertAlmostEqual(row()["verified_age"], 0)
        # The held reading is a copy: changing it does not change the controller.
        row()["verified_native"]["onOff"] = 1
        self.assertEqual(row()["verified_native"]["onOff"], 0)

    def test_command_duration_drives_a_smooth_transition(self):
        self.controller.submit(command(xy=(0.15, 0.06)))
        self.now = 1.0
        target = self.controller.target("left")
        self.assertEqual(target["rgb"], (128, 0, 128))
        self.now = 2.0
        self.assertEqual(self.controller.target("left")["rgb"], (0, 0, 255))

    def test_new_command_starts_at_current_transition_value(self):
        self.controller.submit(command(xy=(0.15, 0.06)))
        self.now = 1.0
        self.controller.submit(command(sequence=2, xy=(0.17, 0.7)))
        self.assertEqual(self.controller.target("left")["rgb"], (128, 0, 128))

    def test_native_off_or_expired_status_blocks_every_command(self):
        self.controller.submit(command(xy=(0.15, 0.06)))
        self.controller.update_statuses({LEFT: ({"onOff": 0}, 0.0)})
        self.controller.render()
        self.assertEqual(self.sent, [])
        self.controller.update_statuses({LEFT: ({"onOff": 1}, 0.0)})
        self.now = 0.8
        self.controller.render()
        self.assertEqual(self.sent, [])

    def test_shared_hue_sync_pause_blocks_output_and_reapplies_after_resume(self):
        self.controller.submit(command(transitiontime=0))
        self.controller.render()
        self.sent.clear()
        self.controller.pause_gate = lambda: False
        self.controller.render()
        self.assertEqual(self.sent, [])
        self.controller.pause_gate = lambda: True
        self.controller.render()
        self.assertTrue(self.sent)

    def test_positive_render_uses_only_color_and_brightness_for_pinned_id(self):
        self.controller.submit(command(xy=(0.15, 0.06)))
        self.now = 0.5
        self.controller.render()
        self.assertTrue(self.sent)
        self.assertEqual({row[0] for row in self.sent}, {LEFT})
        self.assertTrue(all(row[1] in {"colorwc", "brightness"} for row in self.sent))

    def test_udp_send_error_keeps_renderer_alive_and_retries_next_frame(self):
        self.controller.submit(command(transitiontime=0))
        self.controller.sender = lambda *args: (_ for _ in ()).throw(OSError("temporary UDP error"))
        self.controller.render()
        self.assertGreater(self.controller.rejected_sends["left"], 0)
        self.controller.sender = self.send
        self.controller.render()
        self.assertTrue(self.sent)

    def test_per_device_packet_budget_counts_brightness_and_color_together(self):
        calibrated = targets()
        calibrated[0]["max_commands_per_second"] = 10
        sent = []
        controller = adapter.GoveeController(
            calibrated,
            sender=lambda device, cmd, data: sent.append((self.now, cmd)) or True,
            clock=lambda: self.now,
            wall_clock=lambda: 1000,
        )
        controller.update_statuses(self.controller.statuses)
        state = command(xy=(0.15, 0.06))["state"]
        state["bri"] = 127
        controller.submit(command(state=state))
        for tick in range(13):
            self.now = tick * 0.05
            controller.render()
        self.assertEqual(sorted({round(t, 2) for t, _ in sent}), [0, 0.2, 0.4, 0.6])
        self.assertTrue(
            all(sum(t == stamp for t, _ in sent) == 2 for stamp in {t for t, _ in sent})
        )

    def test_invalid_per_device_packet_budget_is_rejected(self):
        for limit in (0, -1, True, float("nan")):
            values = targets()
            values[0]["max_commands_per_second"] = limit
            with self.assertRaises(ValueError):
                adapter.GoveeController(values)

    def test_packet_budget_survives_flapping_power_and_shared_pause(self):
        for mode in ("power", "pause"):
            with self.subTest(mode=mode):
                configured = targets()
                configured[0]["max_commands_per_second"] = 10
                sent = []
                controller = adapter.GoveeController(
                    configured,
                    sender=lambda device, cmd, data: sent.append((self.now, cmd)) or True,
                    clock=lambda: self.now,
                    wall_clock=lambda: 1000,
                )
                controller.submit(command(transitiontime=0))
                for tick in range(20):
                    self.now = tick * 0.05
                    controller.update_statuses(
                        {
                            LEFT: (
                                {
                                    "onOff": int(mode != "power" or tick % 2 == 0),
                                    "brightness": 65,
                                    "color": {"r": 255, "g": 0, "b": 0},
                                },
                                self.now,
                            )
                        }
                    )
                    controller.pause_gate = lambda: mode != "pause" or tick % 2 == 0
                    controller.render()
                self.assertLessEqual(len(sent), 10)

    def test_unknown_side_and_stale_or_replayed_commands_are_rejected(self):
        with self.assertRaises(ValueError):
            self.controller.submit(command(side="third"))
        with self.assertRaises(ValueError):
            self.controller.submit(command(issued_at=990.0))
        self.assertTrue(self.controller.submit(command()))
        self.assertFalse(self.controller.submit(command()))

    def test_virtual_off_stops_output_without_switching_real_power(self):
        self.controller.submit(command(xy=(0.15, 0.06)))
        self.controller.submit(command(sequence=2, state={"on": False}))
        self.controller.render()
        self.assertEqual(self.sent, [])

    def test_static_recall_replaces_dynamic_transition_and_does_not_cycle(self):
        self.controller.submit(command(xy=(0.15, 0.06)))
        self.now = 1.0
        self.controller.submit(command(sequence=2, transitiontime=0))
        self.now = 100
        self.assertEqual(self.controller.target("left")["rgb"], (255, 0, 0))

    def test_static_target_is_reapplied_after_native_off_on(self):
        self.controller.submit(command(transitiontime=0))
        self.controller.render()
        self.sent.clear()
        self.controller.update_statuses({LEFT: ({"onOff": 0}, 0.1)})
        self.now = 0.1
        self.controller.render()
        self.controller.update_statuses(
            {LEFT: ({"onOff": 1, "brightness": 100, "color": {"r": 0, "g": 0, "b": 255}}, 0.2)}
        )
        self.now = 0.2
        self.controller.render()
        self.assertTrue(
            any(
                name == "colorwc" and data["color"] == {"r": 255, "g": 0, "b": 0}
                for _, name, data in self.sent
            )
        )

    def test_invalid_inputs_and_extra_targets_fail_closed(self):
        for value in (float("nan"), -1, 2000):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.controller.submit(command(transitiontime=value))
        with self.assertRaises(ValueError):
            adapter.GoveeController(
                targets()
                + [{"side": "third", "device": "CC:CC:CC:CC:CC:CC:CC:CC", "ip": "192.168.10.99"}]
            )


if __name__ == "__main__":
    unittest.main()
