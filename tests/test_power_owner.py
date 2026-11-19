import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge"))
from live_adapter import GoveeController, handler_for  # noqa: E402
from power_owner import PowerOwner  # noqa: E402
from test_live_adapter import LEFT, RIGHT, targets  # noqa: E402


class PowerOwnerTests(unittest.TestCase):
    def setUp(self):
        self.now = 10.0
        self.wall = 1000.0
        self.gate = PowerOwner(targets(), clock=lambda: self.now, wall_clock=lambda: self.wall)

    def message(self, sequence=1, states=None, session="owner", **extra):
        return {"session": session, "sequence": sequence, "issued_at": self.wall,
                "states": {"left": True, "right": True} if states is None else states, **extra}

    def test_no_grant_off_and_expiry_suppress_even_a_powering_color_sender(self):
        sent = []
        send = self.gate.wrap(lambda device, command, data: sent.append(command) or True)
        right = targets()[1]
        self.assertFalse(send(right, "ptReal", {}))
        self.assertTrue(self.gate.submit(self.message()))
        self.assertTrue(send(right, "ptReal", {}))
        self.assertTrue(self.gate.submit(self.message(2, {"right": False})))
        self.assertFalse(send(right, "ptReal", {}))
        self.now += 2.1
        self.assertFalse(send(targets()[0], "ptReal", {}))
        self.assertEqual(sent, ["ptReal"])

    def test_partial_heartbeat_cannot_extend_other_lamp_grant(self):
        self.gate.submit(self.message())
        self.now += 1.5
        self.wall += 1.5
        self.gate.submit(self.message(2, {"left": True}))
        self.now += .6
        self.assertTrue(self.gate.allowed("left"))
        self.assertFalse(self.gate.allowed("right"))

    def test_old_grant_and_old_session_cannot_undo_off(self):
        self.gate.submit(self.message())
        self.gate.submit(self.message(2, {"right": False}))
        self.assertFalse(self.gate.submit(self.message()))
        self.assertFalse(self.gate.allowed("right"))
        self.assertFalse(self.gate.submit(self.message(session="replacement")))
        self.now += 2.1
        self.assertTrue(self.gate.submit(self.message(session="replacement")))
        self.assertFalse(self.gate.submit(self.message(99)))

    def test_invalid_roles_clock_and_envelopes_never_grant(self):
        for message in (self.message(states={"third": True}),
                        self.message(states={"left": 1}), self.message(states={}),
                        self.message(sequence=True), self.message(extra=True),
                        {**self.message(), "issued_at": float("nan")},
                        {**self.message(), "issued_at": 900}):
            with self.subTest(message=message), self.assertRaises(ValueError):
                self.gate.submit(message)
        self.assertFalse(self.gate.allowed("left"))

    def test_off_ack_waits_for_inflight_color_before_owner_sends_power_off(self):
        entered, release, acknowledged = threading.Event(), threading.Event(), threading.Event()
        events = []

        def sender(*_):
            entered.set()
            release.wait(2)
            events.append("color_completed")
            return True

        self.gate.submit(self.message())
        send = self.gate.wrap(sender)
        writer = threading.Thread(target=send, args=(targets()[1], "ptReal", {}))
        writer.start()
        self.assertTrue(entered.wait(1))

        def off():
            self.gate.submit(self.message(2, {"right": False}))
            events.append("off_acknowledged")
            acknowledged.set()

        owner = threading.Thread(target=off)
        owner.start()
        self.assertFalse(acknowledged.wait(.05))
        release.set()
        writer.join(2)
        owner.join(2)
        self.assertEqual(events, ["color_completed", "off_acknowledged"])
        self.assertFalse(send(targets()[1], "ptReal", {}))

    def test_real_renderer_cannot_reactivate_lamp_after_coordinated_off_with_cached_on(self):
        devices = targets()
        for device in devices:
            device["segment_count"] = 7
        physical = {LEFT: 1, RIGHT: 1}
        sent = []

        def firmware(device, command, _data):
            if command == "ptReal":
                physical[device["device"]] = 1
            sent.append((device["device"], command))
            return True

        controller = GoveeController(devices, sender=self.gate.wrap(firmware),
                                     clock=lambda: self.now, wall_clock=lambda: self.wall)
        controller.update_statuses({key: ({"onOff": 1, "brightness": 65}, self.now)
                                    for key in (LEFT, RIGHT)})
        self.gate.submit(self.message())
        self.gate.submit(self.message(2, {"right": False}))
        physical[RIGHT] = 0
        for side in ("left", "right"):
            controller.submit({"side": side, "session": "scene", "sequence": 1,
                               "issued_at": self.wall, "transitiontime": 0,
                               "state": {"on": True, "segments": [[10, 20, 30]] * 3}})
        controller.render()
        self.assertEqual(physical[RIGHT], 0)
        self.assertTrue(any(device == LEFT for device, _ in sent))
        self.assertFalse(any(device == RIGHT for device, _ in sent))

    def test_http_owner_key_cannot_write_color_and_adapter_key_cannot_grant_power(self):
        controller = GoveeController(targets())
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(
            controller, "adapter-secret", power_owner=self.gate, power_token="owner-secret"))
        worker = threading.Thread(target=server.serve_forever)
        worker.start()

        def request(path, header, key, payload=None):
            req = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}" + path,
                data=None if payload is None else json.dumps(payload).encode(),
                headers={header: key, "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=2) as response:
                    return response.status, json.load(response)
            except urllib.error.HTTPError as error:
                with error:
                    return error.code, json.load(error)

        try:
            self.assertEqual(request("/power-owner", "X-Adapter-Token", "adapter-secret",
                                     self.message())[0], 403)
            self.assertEqual(request("/power-owner", "X-Power-Owner-Token", "owner-secret",
                                      self.message()), (200, {"accepted": True,
                                                              "power_request_version": 1,
                                         "observer_epoch": controller.observer_epoch, "observed": {
                                         "left": {"on": None, "sample": None},
                                         "right": {"on": None, "sample": None},
                                     }}))
            self.assertEqual(request("/command", "X-Power-Owner-Token", "owner-secret", {})[0], 403)
            self.assertEqual(request("/power-owner", "X-Power-Owner-Token", "owner-secret",
                                     self.message(2, {"third": True}))[0], 400)
            code, status = request("/status", "X-Adapter-Token", "adapter-secret")
            self.assertEqual(code, 200)
            self.assertTrue(status["power_owner"]["roles"]["right"]["color_allowed"])
        finally:
            server.shutdown()
            server.server_close()
            worker.join(2)

    def test_power_readback_requires_fresh_identity_proof_and_has_no_device_routes(self):
        controller = GoveeController(targets(), clock=lambda: 10)
        controller.update_statuses({
            LEFT: ({"onOff": 0, "identity_verified": True, "native_sample": 9.95}, 9.9),
            RIGHT: ({"onOff": 1, "identity_verified": True, "native_sample": 9.9}, 9.8),
        })
        self.assertEqual(controller.power_observations(), {
            "left": {"on": False, "sample": 9.95},
            "right": {"on": True, "sample": 9.9},
        })
        for native, seen in (({"onOff": 1}, 10),
                             ({"onOff": True, "identity_verified": True}, 10),
                             ({"onOff": 1, "identity_verified": True}, 9),
                             ({"onOff": 1, "identity_verified": True}, 11)):
            controller.update_statuses({LEFT: (native, seen)})
            self.assertEqual(controller.power_observations()["left"],
                             {"on": None, "sample": None})

    def test_grant_that_expires_waiting_for_writer_lock_is_rejected(self):
        checked = threading.Event()
        errors = []

        def wall():
            value = self.wall
            checked.set()
            return value

        gate = PowerOwner(targets(), clock=lambda: self.now, wall_clock=wall)

        def submit():
            try:
                gate.submit(self.message())
            except ValueError:
                errors.append('expired')

        with gate.lock:
            worker = threading.Thread(target=submit)
            worker.start()
            self.assertTrue(checked.wait(1))
            self.wall += 1
        worker.join(2)
        self.assertEqual(errors, ['expired'])
        self.assertFalse(gate.allowed('right'))

    def test_reusing_adapter_credential_for_power_authority_is_rejected(self):
        with self.assertRaises(ValueError):
            handler_for(GoveeController(targets()), 'same-secret',
                        power_owner=self.gate, power_token='same-secret')

    def test_rapid_off_on_reapplies_zones_even_when_renderer_missed_off_interval(self):
        devices = targets()
        for device in devices:
            device['segment_count'] = 7
        colors = []
        controller = GoveeController(
            devices, sender=self.gate.wrap(
                lambda _device, command, _data: colors.append(command) or True),
            clock=lambda: self.now, wall_clock=lambda: self.wall, power_owner=self.gate)
        controller.update_statuses({RIGHT: ({'onOff': 1, 'brightness': 65}, self.now)})
        self.gate.submit(self.message())
        controller.submit({'side': 'right', 'session': 'scene', 'sequence': 1,
                           'issued_at': self.wall, 'transitiontime': 0,
                           'state': {'on': True, 'segments': [[10, 20, 30]] * 3}})
        controller.render()
        self.assertEqual(colors.count('ptReal'), 1)
        # Power cycling can discard the lamp's zones while the render thread is busy.
        self.gate.submit(self.message(2, {'right': False}))
        self.gate.submit(self.message(3, {'right': True}))
        controller.render()
        self.assertEqual(colors.count('ptReal'), 2)


class PowerHistoryTests(unittest.TestCase):
    def setUp(self):
        self.now = 10.0
        self.wall = 1000.0
        self.gate = PowerOwner(targets(), clock=lambda: self.now, wall_clock=lambda: self.wall)

    def advance(self, seconds):
        self.now += seconds
        self.wall += seconds

    def submit(self, sequence, states):
        return self.gate.submit({"session": "owner", "sequence": sequence,
                                 "issued_at": self.wall, "states": states})

    def events(self):
        return [(e["timestamp"], e["role"], e["cause"], e["requested_on"], e["color_allowed"],
                 e["sync_hold"]) for e in self.gate.snapshot()["events"]]

    def test_heartbeats_record_only_gate_changes_and_expiry_at_its_time(self):
        self.submit(1, {"left": True, "right": True})
        self.advance(1)
        self.submit(2, {"left": True, "right": True})
        self.advance(.5)
        self.submit(3, {"left": True, "right": False})
        self.advance(2.5)
        self.assertEqual(self.events(), [
            ("1970-01-01T00:16:40Z", "left", "owner", True, True, False),
            ("1970-01-01T00:16:40Z", "right", "owner", True, True, False),
            ("1970-01-01T00:16:41.500000Z", "right", "owner", False, False, False),
            # The last heartbeat granted two seconds; the lapse is dated then, not when seen.
            ("1970-01-01T00:16:43.500000Z", "left", "grant_expired", True, False, False),
        ])
        self.advance(1)
        self.submit(4, {"left": True, "right": False})
        self.assertEqual(self.events()[-1][1:], ("left", "owner", True, True, False))

    def test_sync_hold_and_owner_power_are_recorded(self):
        self.submit(1, {"left": True, "right": True})
        self.assertTrue(self.gate.sync_hold("right", send=False))
        self.gate.owner_power("right")
        self.assertEqual([event[1:] for event in self.events()[2:]], [
            ("right", "sync_hold", True, False, True),
            ("right", "owner_power", True, True, False),
        ])

    def test_history_is_bounded(self):
        for sequence in range(1, 200):
            self.submit(sequence, {"right": sequence % 2 == 0})
        events = self.gate.snapshot()["events"]
        self.assertEqual(len(events), 64)
        self.assertEqual(events[-1]["requested_on"], False)
