"""Only successful explicit Hue power writes become selected owner requests."""

import io
import sys
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "experiments/hue-bridge"))
from hue_power_requests import HuePowerRequests, HuePowerUnavailable  # noqa: E402


class HuePowerRequestTests(unittest.TestCase):
    def setUp(self):
        self.calls, self.refreshed = [], []
        self.lights = {slot: SimpleNamespace(id_v1=slot, id_v2="light-" + slot)
                       for slot in ("1", "2", "3")}
        group = SimpleNamespace(id_v1="0", type="LightGroup",
                                getV2GroupedLight=lambda: {"id": "group-all"},
                                lights=[lambda lamp=lamp: lamp for lamp in self.lights.values()])
        self.gateway = HuePowerRequests(
            "http://127.0.0.1:19880", "adapter-key", self.lights, {"0": group},
            SimpleNamespace(refresh_once=lambda **kwargs: self.refreshed.append(True)),
            sender=lambda states: self.calls.append(states),
        )

    def v2(self, kind, rid, payload, status=200):
        return self.gateway.after_request("PUT", f"/clip/v2/resource/{kind}/{rid}", status,
                                          payload, {"data": [{"rid": rid, "rtype": kind}]})

    def test_explicit_v2_light_and_group_use_only_existing_selected_roles(self):
        self.v2("light", "light-2", {"on": {"on": True}})
        self.v2("grouped_light", "group-all", {"on": {"on": False}})
        self.assertEqual(self.calls, [{"right": True}, {"left": False, "right": False}])
        self.assertEqual(len(self.refreshed), 2)

    def test_explicit_v1_light_and_group_route_the_same_roles(self):
        for resource, rid, field in (("lights", "2", "state"), ("groups", "0", "action")):
            address = f"/{resource}/{rid}/{field}/on"
            self.gateway.after_request("PUT", f"/api/key/{resource}/{rid}/{field}", 200,
                                       {"on": True}, [{"success": {address: True}}])
        self.assertEqual(self.calls, [{"right": True}, {"left": True, "right": True}])

    def test_scenes_colors_internal_or_unselected_resources_never_send_power(self):
        self.v2("scene", "scene-id", {"recall": {"action": "active"}, "on": {"on": True}})
        self.v2("light", "light-2", {"dimming": {"brightness": 50}})
        self.v2("light", "light-3", {"on": {"on": True}})
        self.gateway.after_request("PUT", "/api/key/groups/0/action", 200,
                                   {"scene": "scene-id", "on": True}, [])
        self.gateway.after_request("POST", "/clip/v2/resource/light/light-2", 200,
                                   {"on": {"on": True}}, {})
        self.assertEqual(self.calls, [])

    def test_rejected_or_mismatched_vendor_response_cannot_authorize_power(self):
        self.v2("light", "light-2", {"on": {"on": True}}, status=403)
        for response in ({"errors": [{"description": "denied"}]}, {},
                         {"data": [{"rid": "other", "rtype": "light"}]}):
            self.gateway.after_request("PUT", "/clip/v2/resource/light/light-2", 200,
                                       {"on": {"on": True}}, response)
        self.gateway.after_request("PUT", "/api/key/lights/2/state", 200,
                                   {"on": True}, [{"error": {"type": 901}}])
        self.assertEqual(self.calls, [])

    def test_owner_failure_refreshes_actual_state_and_raises_a_safe_error(self):
        def unavailable(states):
            raise OSError("private address or credential")

        self.gateway.sender = unavailable
        with self.assertRaises(HuePowerUnavailable) as caught:
            self.v2("light", "light-2", {"on": {"on": True}})
        self.assertNotIn("private", str(caught.exception))
        self.assertEqual(self.refreshed, [True])

    def test_rejected_owner_http_response_is_closed_without_exposing_details(self):
        response = io.BytesIO(b"private failure details")
        error = urllib.error.HTTPError("http://private", 503, "Unavailable", {}, response)

        def rejected(states):
            raise error

        self.gateway.sender = rejected
        with self.assertRaises(HuePowerUnavailable):
            self.v2("light", "light-2", {"on": {"on": False}})
        self.assertTrue(response.closed)

    def test_hold_and_invalid_power_types_never_reach_owner(self):
        self.gateway.enabled = lambda: False
        with self.assertRaises(HuePowerUnavailable):
            self.v2("light", "light-2", {"on": {"on": True}})
        self.gateway.enabled = lambda: True
        for value in (1, "true", None, [], {}):
            with self.subTest(value=value), self.assertRaises(HuePowerUnavailable):
                self.v2("light", "light-2", {"on": {"on": value}})
        self.assertEqual(self.calls, [])
