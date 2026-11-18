import io
import json
import socket
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import lights_can_communicate_v2 as lighting

LEFT = "AA:AA:AA:AA:AA:AA:AA:AA"
RIGHT = "BB:BB:BB:BB:BB:BB:BB:BB"
THIRD = "CC:CC:CC:CC:CC:CC:CC:CC"


def target():
    return {"device": LEFT, "ip": "192.168.10.50", "side": "left", "brightness_scale": 0.65}


def light():
    return {
        "on": {"on": True},
        "dimming": {"brightness": 100},
        "color": {"xy": {"x": 0.7, "y": 0.299}},
    }


class ScanSocket:
    def __init__(self, ids, now):
        self.ids, self.now = list(ids), now

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def setsockopt(self, *args):
        pass

    def bind(self, *args):
        pass

    def sendto(self, *args):
        pass

    def settimeout(self, *args):
        pass

    def recvfrom(self, *args):
        if not self.ids:
            self.now[0] += 0.2
            raise socket.timeout
        self.now[0] += 0.001
        payload = {"msg": {"cmd": "scan", "data": {"device": self.ids.pop(0)}}}
        return json.dumps(payload).encode(), ("192.168.10.50", 4002)


class IdentityTests(unittest.TestCase):
    def setUp(self):
        for name in ("GOVEE_ROUTES", "VERIFIED_GOVEE", "GOVEE_ROUTE_REVISIONS"):
            context = patch.dict(getattr(lighting, name), {}, clear=True)
            context.start()
            self.addCleanup(context.stop)
        context = patch.object(lighting, "NEXT_GOVEE_DISCOVERY", 0)
        context.start()
        self.addCleanup(context.stop)

    def test_resolution_follows_id_when_old_ip_belongs_to_third_lamp(self):
        result = lighting.resolve_govee_targets(
            [target()],
            [
                {"device": THIRD, "ip": "192.168.10.50"},
                {"device": LEFT, "ip": "192.168.10.80"},
            ],
        )
        self.assertEqual(result, {LEFT: "192.168.10.80"})

    def test_unknown_device_never_replaces_missing_target(self):
        self.assertEqual(
            lighting.resolve_govee_targets([target()], [{"device": THIRD, "ip": "192.168.10.50"}]),
            {},
        )

    def test_ambiguous_identity_or_address_is_rejected(self):
        for discovered in (
            [{"device": LEFT, "ip": "192.168.10.50"}, {"device": LEFT, "ip": "192.168.10.80"}],
            [{"device": LEFT, "ip": "192.168.10.50"}, {"device": THIRD, "ip": "192.168.10.50"}],
        ):
            with self.subTest(discovered=discovered):
                self.assertEqual(lighting.resolve_govee_targets([target()], discovered), {})

    def test_discovery_keeps_conflicting_wire_replies_instead_of_overwriting_them(self):
        now = [10.0]
        with (
            patch.object(lighting.socket, "socket", return_value=ScanSocket([THIRD, LEFT], now)),
            patch.object(lighting.time, "monotonic", side_effect=lambda: now[0]),
        ):
            discovered = lighting.govee_discover(timeout=0.1)
        self.assertEqual({item["device"] for item in discovered}, {LEFT, THIRD})
        self.assertEqual(lighting.resolve_govee_targets([target()], discovered), {})

    def test_unicast_does_not_accept_the_first_of_conflicting_wire_replies(self):
        now = [10.0]
        with (
            patch.object(lighting.socket, "socket", return_value=ScanSocket([LEFT, THIRD], now)),
            patch.object(lighting.time, "monotonic", side_effect=lambda: now[0]),
        ):
            identity = lighting.govee_scan_at("192.168.10.50", timeout=0.1)
        self.assertIsNone(identity)

    def test_unicast_consistent_repeated_identity_is_accepted(self):
        now = [10.0]
        with (
            patch.object(lighting.socket, "socket", return_value=ScanSocket([LEFT, LEFT], now)),
            patch.object(lighting.time, "monotonic", side_effect=lambda: now[0]),
        ):
            identity = lighting.govee_scan_at("192.168.10.50", timeout=0.1)
        self.assertEqual(identity["device"], LEFT)

    def test_unicast_revokes_other_target_when_its_contradiction_arrives_on_this_socket(self):
        now, sent = [10.0], []
        lighting.GOVEE_ROUTES[LEFT] = "192.168.10.50"
        lighting.VERIFIED_GOVEE[LEFT] = ("192.168.10.50", 10)

        class MixedPeers(ScanSocket):
            def recvfrom(self, *args):
                packet, peer = super().recvfrom(*args)
                if json.loads(packet)["msg"]["data"]["device"] == RIGHT:
                    return packet, ("192.168.10.51", 4002)
                return packet, peer

        with (
            patch.object(lighting.socket, "socket", return_value=MixedPeers([THIRD, RIGHT], now)),
            patch.object(lighting.time, "monotonic", side_effect=lambda: now[0]),
            patch.object(lighting, "govee_send", side_effect=lambda *args: sent.append(args)),
        ):
            identity = lighting.govee_scan_at("192.168.10.51", timeout=0.1)
            self.assertEqual(identity["device"], RIGHT)
            self.assertFalse(lighting.send_to_target(target(), "brightness", {"value": 65}))
        self.assertEqual(sent, [])

    def test_control_without_verified_identity_sends_nothing(self):
        sent = []
        with patch.object(lighting, "govee_send", side_effect=lambda *args: sent.append(args)):
            self.assertFalse(lighting.send_to_target(target(), "brightness", {"value": 65}))
        self.assertEqual(sent, [])

    def test_expired_identity_sends_nothing(self):
        lighting.VERIFIED_GOVEE[LEFT] = ("192.168.10.50", 10)
        sent = []
        with (
            patch.object(lighting.time, "monotonic", return_value=10.8),
            patch.object(lighting, "govee_send", side_effect=lambda *args: sent.append(args)),
        ):
            self.assertFalse(lighting.send_to_target(target(), "brightness", {"value": 65}))
        self.assertEqual(sent, [])

    def test_moved_target_is_verified_then_controlled_only_at_new_address(self):
        device = target()
        sent = []
        with (
            patch.object(
                lighting,
                "govee_scan_at",
                side_effect=[
                    {"device": THIRD, "ip": "192.168.10.50"},
                    {"device": LEFT, "ip": "192.168.10.80"},
                ],
            ),
            patch.object(
                lighting,
                "govee_discover",
                return_value=[
                    {"device": THIRD, "ip": "192.168.10.50"},
                    {"device": LEFT, "ip": "192.168.10.80"},
                ],
            ),
            patch.object(lighting, "govee_status", return_value={"onOff": 1}),
            patch.object(lighting, "govee_send", side_effect=lambda *args: sent.append(args)),
        ):
            self.assertEqual(lighting.verified_status(device), {"onOff": 1})
            self.assertTrue(lighting.send_to_target(device, "brightness", {"value": 65}))
        self.assertEqual(sent, [("192.168.10.80", "brightness", {"value": 65})])
        self.assertNotIn(THIRD, lighting.GOVEE_ROUTES)

    def test_observed_wrong_id_revokes_control_before_search(self):
        device = target()
        lighting.VERIFIED_GOVEE[LEFT] = (device["ip"], lighting.time.monotonic())
        sent = []

        def discover(**kwargs):
            self.assertFalse(lighting.send_to_target(device, "brightness", {"value": 65}))
            return [{"device": THIRD, "ip": device["ip"]}]

        with (
            patch.object(
                lighting, "govee_scan_at", return_value={"device": THIRD, "ip": device["ip"]}
            ),
            patch.object(lighting, "govee_discover", side_effect=discover),
            patch.object(lighting, "govee_send", side_effect=lambda *args: sent.append(args)),
        ):
            self.assertIsNone(lighting.verified_status(device))
        self.assertEqual(sent, [])

    def test_search_for_right_revokes_a_contradictory_left_binding(self):
        left = target()
        right = {"device": RIGHT, "ip": "192.168.10.51", "side": "right"}
        lighting.VERIFIED_GOVEE[LEFT] = (left["ip"], lighting.time.monotonic())
        lighting.GOVEE_ROUTES[LEFT] = left["ip"]
        sent = []
        with (
            patch.object(lighting, "govee_scan_at", return_value=None),
            patch.object(
                lighting, "govee_discover", return_value=[{"device": THIRD, "ip": left["ip"]}]
            ),
            patch.object(lighting, "govee_send", side_effect=lambda *args: sent.append(args)),
        ):
            self.assertIsNone(lighting.verified_status(right, targets=[left, right]))
            self.assertFalse(lighting.send_to_target(left, "brightness", {"value": 65}))
        self.assertEqual(sent, [])

    def test_status_read_cannot_reauthorize_after_a_contradictory_scan(self):
        device = target()
        sent = []

        def status(*args, **kwargs):
            lighting.invalidate_govee_observations([{"device": THIRD, "ip": device["ip"]}])
            return {"onOff": 1}

        with (
            patch.object(
                lighting, "govee_scan_at", return_value={"device": LEFT, "ip": device["ip"]}
            ),
            patch.object(lighting, "govee_status", side_effect=status),
            patch.object(lighting, "govee_send", side_effect=lambda *args: sent.append(args)),
        ):
            self.assertIsNone(lighting.verified_status(device))
            self.assertFalse(lighting.send_to_target(device, "brightness", {"value": 65}))
        self.assertEqual(sent, [])

    def test_status_socket_processes_scan_before_status_instead_of_ignoring_it(self):
        now, sent = [10.0], []

        class StatusSocket(ScanSocket):
            def recvfrom(self, *args):
                self.now[0] += 0.001
                if self.ids:
                    return super().recvfrom(*args)
                return lighting.udp_packet("devStatus", {"onOff": 1}), ("192.168.10.50", 4002)

        with (
            patch.object(
                lighting, "govee_scan_at", return_value={"device": LEFT, "ip": "192.168.10.50"}
            ),
            patch.object(lighting.socket, "socket", return_value=StatusSocket([THIRD], now)),
            patch.object(lighting.time, "monotonic", side_effect=lambda: now[0]),
            patch.object(lighting, "govee_send", side_effect=lambda *args: sent.append(args)),
        ):
            self.assertIsNone(lighting.verified_status(target()))
            self.assertFalse(lighting.send_to_target(target(), "brightness", {"value": 65}))
        self.assertEqual(sent, [])

    def test_static_sync_never_controls_a_target_without_identity_confirmation(self):
        config = {"light": "strip", "targets": [target()]}
        sent = []
        with (
            patch.object(lighting, "hue_request", return_value={"data": [light()]}),
            patch.object(lighting, "verified_status", return_value=None),
            patch.object(lighting, "govee_status", return_value={"onOff": 1}),
            patch.object(lighting, "govee_send", side_effect=lambda *args: sent.append(args)),
            redirect_stdout(io.StringIO()),
            self.assertRaises(ValueError),
        ):
            lighting.run_sync(config, once=True)
        self.assertEqual(sent, [])

    def test_animation_cannot_use_cached_power_to_bypass_identity(self):
        device = target()
        sent = []
        with patch.object(lighting, "govee_send", side_effect=lambda *args: sent.append(args)):
            lighting.animate_frame(
                light(), [device], 0, {}, statuses={LEFT: {"onOff": 1}, device["ip"]: {"onOff": 1}}
            )
        self.assertEqual(sent, [])

    def test_unpinned_old_configuration_is_rejected(self):
        config = {"light": "strip", "targets": [{"ip": "192.168.10.50", "side": "left"}]}
        sent = []
        with (
            patch.object(lighting, "hue_request", return_value={"data": [light()]}),
            patch.object(lighting, "govee_status", return_value={"onOff": 1}),
            patch.object(lighting, "govee_send", side_effect=lambda *args: sent.append(args)),
            redirect_stdout(io.StringIO()),
            self.assertRaises(ValueError),
        ):
            lighting.run_sync(config, once=True)
        self.assertEqual(sent, [])

    def test_configure_pins_only_the_two_selected_ids_and_keeps_calibration(self):
        saved = []
        config = {
            "targets": [
                target(),
                {"device": RIGHT, "ip": "192.168.10.51", "side": "right", "brightness_scale": 0.65},
            ]
        }
        with (
            patch(
                "sys.argv",
                [
                    "lights_can_communicate_v2.py",
                    "configure",
                    "--light",
                    "strip",
                    "--left",
                    "192.168.10.50",
                    "--right",
                    "192.168.10.51",
                ],
            ),
            patch.object(lighting, "load_config", return_value=config),
            patch.object(lighting, "save_config", side_effect=lambda value: saved.append(value)),
            patch.object(lighting, "hue_request", return_value={"data": [{"id": "strip"}]}),
            patch.object(
                lighting,
                "govee_discover",
                return_value=[
                    {"device": LEFT, "ip": "192.168.10.50"},
                    {"device": RIGHT, "ip": "192.168.10.51"},
                    {"device": THIRD, "ip": "192.168.10.99"},
                ],
            ),
            redirect_stdout(io.StringIO()),
        ):
            lighting.main()
        self.assertEqual([device.get("device") for device in saved[0]["targets"]], [LEFT, RIGHT])
        self.assertEqual(
            [device.get("brightness_scale") for device in saved[0]["targets"]], [0.65, 0.65]
        )


if __name__ == "__main__":
    unittest.main()
