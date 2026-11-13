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

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "experiments" / "hue-bridge"))
import live_adapter as pilot  # noqa: E402

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


class PilotTests(unittest.TestCase):
    def test_http_routes_each_confirmed_hue_play_role_and_rejects_unknown_role(self):
        from hue_strip_proxy import PLAY_IDS, HueStripProxy
        from test_hue_strip_proxy import FakeHue

        proxies = {}
        for role, resource in PLAY_IDS.items():
            client = FakeHue()
            client.resource_id = resource
            client.native["id"] = resource
            proxies[role] = HueStripProxy(client, wall_clock=lambda: 1000, role=role)
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), pilot.handler_for(self.controller, "test", hue_play_proxies=proxies)
        )
        worker = threading.Thread(target=server.serve_forever)
        worker.start()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            url = "http://127.0.0.1:" + str(server.server_port)
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
        with TemporaryDirectory(prefix="tmp_pilot_hold_") as directory:
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
                pilot.main()

    def setUp(self):
        self.now = 0.0
        self.sent = []
        self.controller = pilot.PilotController(
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
        target = pilot.normalize_state(command()["state"], 0.65)
        self.assertEqual(target["brightness"], 65)
        self.assertEqual(target["rgb"], (255, 0, 0))

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
            pilot.PilotController(
                targets()
                + [{"side": "third", "device": "CC:CC:CC:CC:CC:CC:CC:CC", "ip": "192.168.10.99"}]
            )


if __name__ == "__main__":
    unittest.main()
