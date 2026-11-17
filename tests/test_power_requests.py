"""Authenticated Hue power requests use the existing owner heartbeat, never UDP."""

import json
import shutil
import subprocess
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "experiments/hue-bridge"))
from hue_power_requests import HuePowerRequests  # noqa: E402
from live_adapter import PilotController, handler_for  # noqa: E402
from power_owner import PowerOwner  # noqa: E402
from power_requests import (  # noqa: E402
    PowerRequests,
    PowerRequestUnavailable,
    validate_capability,
)
from test_live_adapter import LEFT, RIGHT, targets  # noqa: E402


class PowerRequestTests(unittest.TestCase):
    def setUp(self):
        self.gate = PowerOwner(targets())
        self.controller = PilotController(targets(), power_owner=self.gate)
        handler = handler_for(self.controller, "adapter-key", power_owner=self.gate,
                              power_token="owner-key")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.worker = threading.Thread(target=self.server.serve_forever)
        self.worker.start()
        self.addCleanup(self.close)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.sequence = 0
        self.revisions = {"left": 0, "right": 0}
        self.states = {"left": False, "right": False}

    def close(self):
        broker = getattr(self.server.RequestHandlerClass, "power_requests", None)
        if broker is not None:
            broker.close()
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(2)

    def post(self, path, body, owner=False, token=None):
        headers = {"Content-Type": "application/json"}
        headers["X-Power-Owner-Token" if owner else "X-Adapter-Token"] = (
            token if token is not None else "owner-key" if owner else "adapter-key"
        )
        request = urllib.request.Request(self.url + path, data=json.dumps(body).encode(),
                                         headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=14) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as response:
            with response:
                return response.code, json.load(response)

    def heartbeat(self, results=(), modern=True):
        self.sequence += 1
        body = {"session": "owner-session", "sequence": self.sequence,
                "issued_at": time.time(), "states": dict(self.states)}
        if modern:
            body["power_requests"] = {"version": 1, "revisions": dict(self.revisions),
                                      "results": list(results)}
        return self.post("/power-owner", body, owner=True)

    def begin(self, states=None):
        body = {"id": "a" * 32, "issued_at": time.time(),
                "states": states or {"right": True}}
        result = []
        worker = threading.Thread(target=lambda: result.append(self.post("/power-request", body)))
        worker.start()
        self.addCleanup(worker.join, 2)
        return body, result, worker

    def command(self):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            status, response = self.heartbeat()
            self.assertEqual(status, 200, response)
            if response.get("power_requests"):
                return response["power_requests"][0]
            time.sleep(0.01)
        self.fail("Owner did not receive the explicit power request")

    def native(self, right):
        now = time.monotonic()
        self.controller.update_statuses({
            key: ({"onOff": int(value), "identity_verified": True, "native_sample": now}, now)
            for key, value in ((LEFT, False), (RIGHT, right))
        })

    def test_legacy_owner_is_compatible_but_cannot_claim_remote_power_support(self):
        status, capability = self.heartbeat(modern=False)
        self.assertEqual(status, 200)
        self.assertEqual(capability["power_request_version"], 1)
        status, body = self.post("/power-request", {"id": "a" * 32,
                                "issued_at": time.time(), "states": {"right": True}})
        self.assertEqual(status, 503, body)
        self.assertFalse(body["accepted"])

    def test_explicit_request_reaches_owner_and_waits_for_fresh_native_confirmation(self):
        self.assertEqual(self.heartbeat()[0], 200)
        body, result, worker = self.begin()
        command = self.command()
        self.assertEqual(command["role"], "right")
        self.assertIs(command["on"], True)
        self.assertEqual(command["revision"], 0)
        self.assertEqual(set(command), {"id", "role", "on", "issued_at", "expires_at", "revision"})
        self.assertEqual(result, [])
        self.native(True)
        self.revisions["right"] = 1
        self.states["right"] = True
        status, ack = self.heartbeat([{"id": command["id"], "success": True}])
        self.assertEqual(status, 200, ack)
        self.assertIn(command["id"], ack["power_request_acks"])
        worker.join(2)
        self.assertEqual(result, [(200, {"accepted": True})])
        self.assertEqual(self.post("/power-request", body), (200, {"accepted": True}))
        self.assertEqual(self.heartbeat()[1]["power_requests"], [])

    def test_success_without_matching_native_proof_is_rejected(self):
        self.assertEqual(self.heartbeat()[0], 200)
        _, result, worker = self.begin()
        command = self.command()
        self.native(False)
        self.revisions["right"] = 1
        self.states["right"] = True
        self.heartbeat([{"id": command["id"], "success": True}])
        worker.join(2)
        self.assertEqual(result[0][0], 503)
        self.assertFalse(result[0][1]["accepted"])

    def test_malformed_capability_does_not_renew_a_power_grant(self):
        body = {"session": "owner-session", "sequence": 1, "issued_at": time.time(),
                "states": {"right": True}, "power_requests": {
                    "version": True, "revisions": {"right": 0}, "results": []}}
        self.assertEqual(self.post("/power-owner", body, owner=True)[0], 400)
        self.assertFalse(self.gate.allowed("right"))

    def test_non_object_owner_json_returns_a_validation_error(self):
        self.assertEqual(self.post("/power-owner", None, owner=True)[0], 400)

    def test_wrong_credential_or_unknown_role_never_creates_a_command(self):
        body = {"id": "a" * 32, "issued_at": time.time(), "states": {"right": True}}
        self.assertEqual(self.post("/power-request", body, token="owner-key")[0], 403)
        body["states"] = {"third": True}
        self.assertEqual(self.post("/power-request", body)[0], 400)
        self.assertFalse(self.gate.allowed("left"))
        self.assertFalse(self.gate.allowed("right"))


@unittest.skipUnless(shutil.which("node"), "Node required for real owner loopback")
class PowerRequestOwnerIntegrationTests(unittest.TestCase):
    def test_hue_commands_use_coordinated_setter_with_actual_heartbeat_and_native_proof(self):
        gate = PowerOwner(targets())
        controller = PilotController(targets(), power_owner=gate)
        physical = {"left": False, "right": False, "third": True}
        writes = []
        parent = handler_for(controller, "adapter-key", power_owner=gate, power_token="a" * 48)

        class Handler(parent):
            def do_POST(self):
                if self.path == "/test-hue":
                    value = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    gateway = HuePowerRequests(f"http://127.0.0.1:{self.server.server_port}",
                                               "adapter-key", {}, {}, None)
                    gateway.send(value["states"])
                    return self.respond(200, {"accepted": True})
                if self.path == "/test-power":
                    value = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    role = value["role"]
                    assert role in {"left", "right"}
                    assert not gate.allowed(role), "Power changed while color grant remained open"
                    physical[role] = value["on"]
                    writes.append((role, value["on"]))
                    return self.respond(200, {})
                if self.path == "/power-owner":
                    now = time.monotonic()
                    controller.update_statuses({
                        device: ({"onOff": int(physical[role]), "identity_verified": True,
                                  "native_sample": now}, now)
                        for role, device in (("left", LEFT), ("right", RIGHT))
                    })
                return super().do_POST()

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever)
        worker.start()
        try:
            fixture = Path(__file__).parent / "fixtures/hue_power_owner_flow.mjs"
            result = subprocess.run([shutil.which("node"), str(fixture),
                                     f"http://127.0.0.1:{server.server_port}"],
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(writes[:2], [("right", True), ("right", False)])
            self.assertCountEqual(writes[2:4], [("left", True), ("right", True)])
            self.assertEqual(writes[4:], [("right", False)])
            self.assertEqual(physical, {"left": True, "right": False, "third": True})
        finally:
            parent.power_requests.close()
            server.shutdown()
            server.server_close()
            worker.join(2)


class BrokerFenceTests(unittest.TestCase):
    def setUp(self):
        self.now, self.wall = 100, 1000
        self.owner = PowerOwner(targets(), clock=lambda: self.now, wall_clock=lambda: self.wall)
        self.broker = PowerRequests(self.owner)
        self.workers = []
        self.addCleanup(self.close)
        self.sequence = 0
        self.revisions = {"left": 0, "right": 0}
        self.session = "owner"
        self.exchange()

    def close(self):
        self.broker.close()
        for worker in self.workers:
            worker.join(1)
            self.assertFalse(worker.is_alive())

    def exchange(self, results=(), observed=None):
        self.sequence += 1
        body = {"session": self.session, "sequence": self.sequence, "issued_at": self.wall,
                "states": {"left": False, "right": False}}
        extension = {"version": 1, "revisions": dict(self.revisions), "results": list(results)}
        accepted = self.owner.submit(body)
        return self.broker.exchange(body, validate_capability(extension, body["states"]),
                                    observed or {}, accepted)

    def start(self, states=None, request_id="a" * 32):
        result = []

        def send():
            try:
                result.append(self.broker.request({"id": request_id, "issued_at": self.wall,
                                                  "states": states or {"right": True}}))
            except PowerRequestUnavailable:
                result.append(False)

        worker = threading.Thread(target=send)
        self.workers.append(worker)
        worker.start()
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            commands = self.exchange()["power_requests"]
            if commands or result:
                return result, commands, worker
            time.sleep(0.005)
        self.fail("Request did not reach its observable owner boundary")

    def test_expired_lease_cancels_request_without_delivery_on_reconnect(self):
        result, _, worker = self.start()
        self.now += 3
        self.wall += 3
        worker.join(1)
        self.assertEqual(result, [False])
        self.assertEqual(self.exchange()["power_requests"], [])

    def test_new_owner_session_cannot_receive_an_old_request(self):
        result, _, worker = self.start()
        self.now += 3
        self.wall += 3
        self.session = "replacement"
        self.assertEqual(self.exchange()["power_requests"], [])
        worker.join(1)
        self.assertEqual(result, [False])

    def test_superseding_home_revision_prevents_old_on_completion(self):
        result, commands, worker = self.start()
        self.revisions["right"] = 2
        self.assertEqual(self.exchange()["power_requests"], [])
        worker.join(1)
        self.assertEqual(result, [False])
        self.assertFalse(self.owner.allowed("right"))
        self.assertEqual(len(commands), 1)

    def test_delayed_older_heartbeat_cannot_confirm_on_after_newer_off_is_accepted(self):
        result, commands, worker = self.start()
        command = commands[0]
        self.sequence += 1
        older = {"session": self.session, "sequence": self.sequence, "issued_at": self.wall,
                 "states": {"left": False, "right": True}}
        self.assertTrue(self.owner.submit(older))
        self.sequence += 1
        newer = {**older, "sequence": self.sequence,
                 "states": {"left": False, "right": False}}
        self.assertTrue(self.owner.submit(newer))
        capability = {"version": 1, "revisions": {"left": 0, "right": 1},
                      "results": [{"id": command["id"], "success": True}]}
        self.broker.exchange(older, capability, {"right": {"on": True, "sample": 1}}, True)
        worker.join(0.1)
        self.assertEqual(result, [])
        self.revisions["right"] = 2
        self.exchange()
        worker.join(1)
        self.assertEqual(result, [False])
        self.assertFalse(self.owner.allowed("right"))

    def test_failed_group_keeps_a_delivered_other_role_busy_until_completion(self):
        result, commands, worker = self.start({"left": True, "right": True})
        right = next(command for command in commands if command["role"] == "right")
        self.exchange([{"id": right["id"], "success": False}])
        worker.join(1)
        self.assertEqual(result, [False])
        second, outgoing, other = self.start({"left": True}, request_id="b" * 32)
        self.assertEqual(outgoing, [])
        other.join(1)
        self.assertEqual(second, [False])

    def test_stale_or_unknown_requests_fail_before_owner_dispatch(self):
        for changes in ({"issued_at": 999}, {"issued_at": True}, {"id": "A" * 32},
                        {"states": {"third": True}}, {"states": {"right": 1}}):
            body = {"id": "a" * 32, "issued_at": self.wall, "states": {"right": True},
                    **changes}
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.broker.request(body)
        self.assertEqual(self.exchange()["power_requests"], [])

    def test_wire_lifetime_uses_one_wall_clock_sample(self):
        ticks = [1000.0]

        def advancing_wall():
            ticks[0] += 0.0001
            return ticks[0]

        self.broker.wall_clock = advancing_wall
        _, commands, _ = self.start()
        command = commands[0]
        self.assertLessEqual(command["expires_at"] - command["issued_at"], 12)

    def test_completed_id_is_not_reexecuted_when_active_deadline_has_elapsed(self):
        result, commands, worker = self.start({"right": False})
        self.revisions["right"] = 1
        self.exchange([{"id": commands[0]["id"], "success": True}],
                      observed={"right": {"on": False, "sample": 1}})
        worker.join(1)
        self.assertEqual(result, [True])
        self.now += 13
        self.wall += 13
        self.exchange()
        repeated, commands, again = self.start({"right": False})
        self.assertEqual(commands, [])
        again.join(1)
        self.assertEqual(repeated, [True])

    def test_rejected_new_request_cannot_evict_a_completed_replay_fence(self):
        for number in range(64):
            result, commands, worker = self.start({"right": False}, f"{number:032x}")
            self.revisions["right"] += 1
            self.exchange([{"id": commands[0]["id"], "success": True}],
                          observed={"right": {"on": False, "sample": number + 1}})
            worker.join(1)
            self.assertEqual(result, [True])
        self.now += 3
        self.wall += 3
        with self.assertRaises(PowerRequestUnavailable):
            self.broker.request({"id": "f" * 32, "issued_at": self.wall,
                                 "states": {"right": False}})
        self.exchange()
        repeated, outgoing, worker = self.start({"right": False}, "0" * 32)
        self.assertEqual(outgoing, [])
        worker.join(1)
        self.assertEqual(repeated, [True])

    def test_full_cache_rejects_new_ids_while_original_payloads_remain_valid(self):
        first = {"id": "0" * 32, "issued_at": self.wall, "states": {"right": False}}
        for number in range(64):
            result, commands, worker = self.start({"right": False}, f"{number:032x}")
            self.revisions["right"] += 1
            self.exchange([{"id": commands[0]["id"], "success": True}],
                          observed={"right": {"on": False, "sample": number + 1}})
            worker.join(1)
            self.assertEqual(result, [True])
        refused, outgoing, worker = self.start({"right": False}, "f" * 32)
        self.assertEqual(outgoing, [])
        worker.join(1)
        self.assertEqual(refused, [False])
        self.assertTrue(self.broker.request(first))
        self.assertEqual(self.exchange()["power_requests"], [])
        self.now += 0.4
        self.wall += 0.4
        renewed = {**first, "issued_at": self.wall}
        self.assertTrue(self.broker.request(renewed))
        self.now += 0.2
        self.wall += 0.2
        accepted, outgoing, worker = self.start({"right": False}, "e" * 32)
        self.assertEqual(len(outgoing), 1)
        self.assertTrue(self.broker.request(renewed))
        with self.assertRaises(ValueError):
            self.broker.request(first)
