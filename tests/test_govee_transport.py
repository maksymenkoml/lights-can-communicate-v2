import json
import socket
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge"))
from govee_transport import GoveeTransport  # noqa: E402
from live_adapter import GoveeController  # noqa: E402
from power_owner import PowerOwner  # noqa: E402

import lights_can_communicate_v2 as lan  # noqa: E402

LEFT = "AA:AA:AA:AA:AA:AA:AA:AA"
RIGHT = "BB:BB:BB:BB:BB:BB:BB:BB"
THIRD = "CC:CC:CC:CC:CC:CC:CC:CC"


class GoveeTransportTests(unittest.TestCase):
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
            patch("govee_transport.socket.socket", return_value=sock),
            patch("govee_transport.time.monotonic", side_effect=lambda: now[0]),
            patch(
                "govee_transport.select.select",
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
        self.transport = GoveeTransport(
            self.targets, publisher=lambda value: self.updates.append(value)
        )

    def message(self, cmd, data, ip, now):
        self.transport.handle({"msg": {"cmd": cmd, "data": data}}, ip, now)

    def controls(self, now, command="colorwc"):
        """Whether a command to the left lamp would be sent at ``now``."""
        with patch.object(lan, "govee_send", lambda *args: None), \
                patch.object(lan.time, "monotonic", return_value=now):
            return lan.send_to_target(self.targets[0], command, {})

    def test_status_alone_never_authorizes_control(self):
        self.message("devStatus", {"onOff": 1}, "192.168.10.50", 10)
        self.assertNotIn(LEFT, lan.VERIFIED_GOVEE)

    def test_fresh_identity_and_native_on_authorize_only_pinned_id(self):
        self.message("scan", {"device": LEFT}, "192.168.10.50", 10)
        self.message("devStatus", {"onOff": 1}, "192.168.10.50", 10.02)
        self.assertEqual(lan.VERIFIED_GOVEE[LEFT], ("192.168.10.50", 10.02))
        self.message("scan", {"device": THIRD}, "192.168.10.99", 10.03)
        self.assertNotIn(THIRD, lan.VERIFIED_GOVEE)

    def test_an_off_lamp_can_be_switched_on_but_gets_no_color(self):
        sent = []
        self.message("scan", {"device": LEFT}, "192.168.10.50", 10)
        self.message("devStatus", {"onOff": 0}, "192.168.10.50", 10.02)
        self.assertNotIn(LEFT, lan.VERIFIED_GOVEE)
        self.assertEqual(lan.IDENTIFIED_GOVEE[LEFT], ("192.168.10.50", 10.02))
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
        # The contradiction is held as long as an identity: still withdrawn.
        self.message("scan", {"device": LEFT}, "192.168.10.50", 11)
        self.message("devStatus", {"onOff": 0}, "192.168.10.50", 11.02)
        self.assertNotIn(LEFT, lan.IDENTIFIED_GOVEE)
        self.message("scan", {"device": LEFT}, "192.168.10.50", 15.2)
        self.message("devStatus", {"onOff": 0}, "192.168.10.50", 15.22)
        self.assertIn(LEFT, lan.IDENTIFIED_GOVEE)
        # A fresh status keeps the route while the identity is held.
        self.message("devStatus", {"onOff": 0}, "192.168.10.50", 20.1)
        self.assertIn(LEFT, lan.IDENTIFIED_GOVEE)
        # No identity for longer than the hold: withdrawn as well.
        self.transport.reconcile(20.21)
        self.assertNotIn(LEFT, lan.IDENTIFIED_GOVEE)

    def test_scan_refresh_does_not_manufacture_a_new_native_power_sample(self):
        controller = GoveeController(self.targets, clock=lambda: 10.18)
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
        self.assertFalse(self.controls(10.8))

    def test_ip_change_keeps_identity_and_never_reuses_old_address(self):
        self.message("scan", {"device": LEFT}, "192.168.10.80", 10)
        self.message("devStatus", {"onOff": 1}, "192.168.10.80", 10.02)
        self.assertEqual(lan.VERIFIED_GOVEE[LEFT][0], "192.168.10.80")
        self.assertEqual(self.targets[0]["ip"], "192.168.10.80")
        self.message("scan", {"device": THIRD}, "192.168.10.50", 10.03)
        self.assertEqual(lan.VERIFIED_GOVEE[LEFT][0], "192.168.10.80")

    def test_identity_is_held_through_missed_scans_but_status_must_be_fresh(self):
        # The older H6076 answers about half of the scans; its devStatus replies
        # keep coming. A proven identity is held for IDENTITY_SECONDS.
        self.assertEqual(GoveeTransport.IDENTITY_SECONDS, 5)
        self.message("scan", {"device": LEFT}, "192.168.10.50", 10)
        for now in (10.2, 11, 12.5):
            self.message("devStatus", {"onOff": 1}, "192.168.10.50", now)
            self.assertEqual(lan.VERIFIED_GOVEE[LEFT], ("192.168.10.50", now))
        # The proof time is the status sample: a missed status still refuses color.
        self.transport.reconcile(13.3)
        self.assertEqual(lan.VERIFIED_GOVEE[LEFT], ("192.168.10.50", 12.5))
        self.assertFalse(self.controls(13.3))
        self.message("devStatus", {"onOff": 1}, "192.168.10.50", 14.9)
        self.assertEqual(lan.VERIFIED_GOVEE[LEFT], ("192.168.10.50", 14.9))
        # A fresh status does not extend the identity beyond the hold.
        self.message("devStatus", {"onOff": 1}, "192.168.10.50", 15.01)
        self.assertNotIn(LEFT, lan.VERIFIED_GOVEE)
        self.assertNotIn(LEFT, lan.IDENTIFIED_GOVEE)
        # A new scan proves it again.
        self.message("scan", {"device": LEFT}, "192.168.10.50", 15.1)
        self.assertEqual(lan.VERIFIED_GOVEE[LEFT], ("192.168.10.50", 15.01))

    def test_dreamview_frames_ride_out_a_short_status_miss_and_nothing_else_does(self):
        sent = []
        device = self.targets[0]

        def attempt(now, command="razer"):
            with patch.object(lan, "govee_send", lambda ip, name, data: sent.append(name)), \
                    patch.object(lan.time, "monotonic", return_value=now):
                return lan.send_to_target(device, command, {})

        self.assertEqual(lan.STREAM_HOLD_SECONDS, 1.5)
        self.message("scan", {"device": LEFT}, "192.168.10.50", 10)
        self.message("devStatus", {"onOff": 1}, "192.168.10.50", 10.02)
        # Status replies go missing: the binding stays with its old proof time.
        self.transport.reconcile(11)
        self.assertEqual(lan.VERIFIED_GOVEE[LEFT], ("192.168.10.50", 10.02))
        self.assertTrue(attempt(11.5))
        self.assertFalse(attempt(11.5, "colorwc"))
        self.assertFalse(attempt(11.5, "brightness"))
        self.assertFalse(attempt(11.53))
        self.assertEqual(sent, ["razer"])
        # A verified Off stops frames at once.
        self.message("devStatus", {"onOff": 1}, "192.168.10.50", 12)
        self.assertTrue(attempt(12.1))
        self.message("devStatus", {"onOff": 0}, "192.168.10.50", 12.2)
        self.assertFalse(attempt(12.3))
        # So does a lost identity.
        self.message("devStatus", {"onOff": 1}, "192.168.10.50", 12.4)
        self.message("scan", {"device": THIRD}, "192.168.10.50", 12.5)
        self.assertFalse(attempt(12.6))

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
        controller = GoveeController(self.targets, clock=lambda: now[0], wall_clock=lambda: 1000)
        transport = GoveeTransport(controller.targets, controller.update_statuses)
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
