import json
import socket
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge"))
from live_adapter import PilotController  # noqa: E402
from pilot_transport import PilotTransport  # noqa: E402
from power_owner import PowerOwner  # noqa: E402

import lights_can_communicate_v2 as lan  # noqa: E402

LEFT = "AA:AA:AA:AA:AA:AA:AA:AA"
RIGHT = "BB:BB:BB:BB:BB:BB:BB:BB"
THIRD = "CC:CC:CC:CC:CC:CC:CC:CC"


class PilotTransportTests(unittest.TestCase):
    def test_new_candidate_ip_gets_read_only_status_before_control_is_authorized(self):
        now = [10.0]
        stop = threading.Event()
        sent = []
        queue = [(lan.udp_packet("scan", {"device": RIGHT}), ("192.168.10.80", 4002))]
        case = self

        class FakeSocket:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def setsockopt(self, *args):
                pass

            def bind(self, *args):
                pass

            def settimeout(self, *args):
                pass

            def sendto(self, raw, dest):
                command = json.loads(raw)["msg"]["cmd"]
                sent.append((command, dest))
                if command == "devStatus" and dest[0] == "192.168.10.80":
                    case.assertNotIn(RIGHT, lan.VERIFIED_GOVEE)
                    queue.append(
                        (lan.udp_packet("devStatus", {"onOff": 1}), ("192.168.10.80", 4002))
                    )

            def recvfrom(self, *args):
                now[0] += 0.05
                if queue:
                    value = queue.pop(0)
                    if json.loads(value[0])["msg"]["cmd"] == "devStatus":
                        stop.set()
                    return value
                if now[0] >= 10.65:
                    stop.set()
                raise socket.timeout()

        sock = FakeSocket()
        with (
            patch("pilot_transport.socket.socket", return_value=sock),
            patch("pilot_transport.time.monotonic", side_effect=lambda: now[0]),
            patch(
                "pilot_transport.select.select",
                side_effect=lambda *args: ([sock], [], []) if queue else ([], [], []),
            ),
        ):
            self.transport.receive_loop(stop)
        self.assertIn(("devStatus", ("192.168.10.80", 4003)), sent)
        self.assertEqual(self.targets[1]["ip"], "192.168.10.80")

    def setUp(self):
        for name in ("GOVEE_ROUTES", "VERIFIED_GOVEE", "IDENTIFIED_GOVEE",
                     "GOVEE_ROUTE_REVISIONS"):
            context = patch.dict(getattr(lan, name), {}, clear=True)
            context.start()
            self.addCleanup(context.stop)
        self.targets = [
            {"device": LEFT, "side": "left", "ip": "192.168.10.50"},
            {"device": RIGHT, "side": "right", "ip": "192.168.10.51"},
        ]
        self.updates = []
        self.transport = PilotTransport(
            self.targets, publisher=lambda value: self.updates.append(value)
        )

    def message(self, cmd, data, ip, now):
        self.transport.handle({"msg": {"cmd": cmd, "data": data}}, ip, now)

    def test_status_alone_never_authorizes_control(self):
        self.message("devStatus", {"onOff": 1}, "192.168.10.50", 10)
        self.assertNotIn(LEFT, lan.VERIFIED_GOVEE)

    def test_fresh_identity_and_native_on_authorize_only_pinned_id(self):
        self.message("scan", {"device": LEFT}, "192.168.10.50", 10)
        self.message("devStatus", {"onOff": 1}, "192.168.10.50", 10.02)
        self.assertEqual(lan.VERIFIED_GOVEE[LEFT], ("192.168.10.50", 10))
        self.message("scan", {"device": THIRD}, "192.168.10.99", 10.03)
        self.assertNotIn(THIRD, lan.VERIFIED_GOVEE)

    def test_an_off_lamp_can_be_switched_on_but_gets_no_color(self):
        sent = []
        self.message("scan", {"device": LEFT}, "192.168.10.50", 10)
        self.message("devStatus", {"onOff": 0}, "192.168.10.50", 10.02)
        self.assertNotIn(LEFT, lan.VERIFIED_GOVEE)
        self.assertEqual(lan.IDENTIFIED_GOVEE[LEFT], ("192.168.10.50", 10))
        device = self.targets[0]
        with patch.object(lan, "govee_send", lambda ip, name, data: sent.append((ip, name))), \
                patch.object(lan.time, "monotonic", return_value=10.3):
            self.assertFalse(lan.send_power_to_target(device, "colorwc", {}))
            self.assertFalse(lan.send_to_target(device, "turn", {"value": 1}))
            self.assertTrue(lan.send_power_to_target(device, "turn", {"value": 1}))
        self.assertEqual(sent, [("192.168.10.50", "turn")])
        # A stale proof sends nothing.
        with patch.object(lan, "govee_send", lambda ip, name, data: sent.append((ip, name))), \
                patch.object(lan.time, "monotonic", return_value=11):
            self.assertFalse(lan.send_power_to_target(device, "turn", {"value": 1}))
        self.assertEqual(len(sent), 1)

    def test_a_sync_hold_restores_an_off_lamp_over_the_power_route(self):
        import time

        native = {"on": False, "sample": None}

        def send(ip, name, data):
            native.update(on=data["value"] == 1, sample=time.monotonic())

        owner = PowerOwner(self.targets)
        owner.attach_power(lan.send_power_to_target, observe=lambda: {"left": dict(native)},
                           sleep=lambda seconds: None)
        owner.sync_hold("left", send=False)
        lan.IDENTIFIED_GOVEE[LEFT] = ("192.168.10.50", time.monotonic())
        with patch.object(lan, "govee_send", send):
            self.assertEqual(owner.sync_release("left"), "restored")
        self.assertTrue(native["on"])

    def test_a_lost_identity_withdraws_the_power_route(self):
        self.message("scan", {"device": LEFT}, "192.168.10.50", 10)
        self.message("devStatus", {"onOff": 0}, "192.168.10.50", 10.02)
        self.assertIn(LEFT, lan.IDENTIFIED_GOVEE)
        # Another device answers at that address: the route is withdrawn.
        self.message("scan", {"device": THIRD}, "192.168.10.50", 10.1)
        self.assertNotIn(LEFT, lan.IDENTIFIED_GOVEE)
        self.message("scan", {"device": LEFT}, "192.168.10.50", 11)
        self.message("devStatus", {"onOff": 0}, "192.168.10.50", 11.02)
        self.assertIn(LEFT, lan.IDENTIFIED_GOVEE)
        # No fresh identity after 0.75 s: withdrawn as well.
        self.transport.reconcile(12)
        self.assertNotIn(LEFT, lan.IDENTIFIED_GOVEE)

    def test_scan_refresh_does_not_manufacture_a_new_native_power_sample(self):
        controller = PilotController(self.targets, clock=lambda: 10.18)
        self.transport.publisher = controller.update_statuses
        self.message("scan", {"device": LEFT}, "192.168.10.50", 10)
        self.message("devStatus", {"onOff": 1}, "192.168.10.50", 10.01)
        first = controller.power_observations()["left"]
        self.message("scan", {"device": LEFT}, "192.168.10.50", 10.17)
        second = controller.power_observations()["left"]
        self.assertEqual(first, {"on": True, "sample": 10.01})
        self.assertEqual(second, first)

    def test_contradictory_peer_identity_revokes_other_target_immediately(self):
        self.message("scan", {"device": LEFT}, "192.168.10.50", 10)
        self.message("devStatus", {"onOff": 1}, "192.168.10.50", 10.02)
        self.message("scan", {"device": THIRD}, "192.168.10.50", 10.03)
        self.assertNotIn(LEFT, lan.VERIFIED_GOVEE)
        self.message("scan", {"device": LEFT}, "192.168.10.50", 10.04)
        self.assertNotIn(LEFT, lan.VERIFIED_GOVEE)

    def test_native_off_and_stale_identity_revoke_control(self):
        self.message("scan", {"device": LEFT}, "192.168.10.50", 10)
        self.message("devStatus", {"onOff": 1}, "192.168.10.50", 10.02)
        self.message("devStatus", {"onOff": 0}, "192.168.10.50", 10.03)
        self.assertNotIn(LEFT, lan.VERIFIED_GOVEE)
        self.message("devStatus", {"onOff": 1}, "192.168.10.50", 10.04)
        self.transport.reconcile(10.8)
        self.assertNotIn(LEFT, lan.VERIFIED_GOVEE)

    def test_ip_change_keeps_identity_and_never_reuses_old_address(self):
        self.message("scan", {"device": LEFT}, "192.168.10.80", 10)
        self.message("devStatus", {"onOff": 1}, "192.168.10.80", 10.02)
        self.assertEqual(lan.VERIFIED_GOVEE[LEFT][0], "192.168.10.80")
        self.assertEqual(self.targets[0]["ip"], "192.168.10.80")
        self.message("scan", {"device": THIRD}, "192.168.10.50", 10.03)
        self.assertEqual(lan.VERIFIED_GOVEE[LEFT][0], "192.168.10.80")

    def test_power_refresh_does_not_extend_old_identity_confirmation(self):
        self.message("scan", {"device": LEFT}, "192.168.10.50", 10)
        self.message("devStatus", {"onOff": 1}, "192.168.10.50", 10.6)
        self.assertEqual(lan.VERIFIED_GOVEE[LEFT][1], 10)
        self.transport.reconcile(10.76)
        self.assertNotIn(LEFT, lan.VERIFIED_GOVEE)

    def test_explicit_off_revokes_before_deferred_batch_publish(self):
        self.message("scan", {"device": LEFT}, "192.168.10.50", 10)
        self.message("devStatus", {"onOff": 1}, "192.168.10.50", 10.02)
        self.transport.handle(
            {"msg": {"cmd": "devStatus", "data": {"onOff": 0}}},
            "192.168.10.50",
            10.03,
            publish=False,
        )
        sent = []
        with (
            patch.object(lan.time, "monotonic", return_value=10.03),
            patch.object(lan, "govee_send", side_effect=lambda *args: sent.append(args)),
        ):
            self.assertFalse(lan.send_to_target(self.targets[0], "brightness", {"value": 65}))
        self.assertEqual(sent, [])

    def test_mocked_continuous_transport_keeps_rendering_without_physical_network(self):
        now = [10.0]
        sent = []
        for item in self.targets:
            item["brightness_scale"] = 0.65
        controller = PilotController(self.targets, clock=lambda: now[0], wall_clock=lambda: 1000)
        transport = PilotTransport(controller.targets, controller.update_statuses)
        with (
            patch.object(lan.time, "monotonic", side_effect=lambda: now[0]),
            patch.object(
                lan,
                "govee_send",
                side_effect=lambda ip, cmd, data: sent.append((now[0], ip, cmd, data)),
            ),
        ):
            for tick in range(40):
                now[0] = 10 + tick * 0.05
                if tick % 4 == 0:
                    for item in controller.targets:
                        transport.handle(
                            {"msg": {"cmd": "scan", "data": {"device": item["device"]}}},
                            item["ip"],
                            now[0],
                        )
                        transport.handle(
                            {
                                "msg": {
                                    "cmd": "devStatus",
                                    "data": {
                                        "onOff": 1,
                                        "brightness": 65,
                                        "color": {"r": 255, "g": 0, "b": 0},
                                        "colorTemInKelvin": 0,
                                    },
                                }
                            },
                            item["ip"],
                            now[0],
                        )
                if tick == 0:
                    controller.submit(
                        {
                            "side": "left",
                            "session": "offline",
                            "sequence": 1,
                            "issued_at": 1000,
                            "transitiontime": 20,
                            "state": {
                                "on": True,
                                "bri": 254,
                                "colormode": "xy",
                                "xy": [0.15, 0.06],
                            },
                        }
                    )
                controller.render()
        colors = [row for row in sent if row[2] == "colorwc"]
        self.assertGreaterEqual(len(colors), 30)
        self.assertLessEqual(max(b[0] - a[0] for a, b in zip(colors, colors[1:])), 0.051)
        self.assertEqual({row[1] for row in sent}, {"192.168.10.50"})
        self.assertNotIn("turn", {row[2] for row in sent})


if __name__ == "__main__":
    unittest.main()
