"""Only successful explicit Hue power writes become selected owner requests."""

import io
import sys
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge"))
from hue_power_requests import HuePowerRequests, HuePowerUnavailable  # noqa: E402


class HuePowerRequestTests(unittest.TestCase):
    def setUp(self):
        self.calls, self.refreshed, self.native, self.feedback_calls = [], [], [], []
        self.lights = {slot: SimpleNamespace(id_v1=slot, id_v2="light-" + slot)
                       for slot in ("1", "2", "3", "4", "7")}
        group = SimpleNamespace(id_v1="0", type="LightGroup",
                                getV2GroupedLight=lambda: {"id": "group-all"},
                                lights=[lambda lamp=lamp: lamp for lamp in self.lights.values()])
        self.gateway = HuePowerRequests(
            "http://127.0.0.1:19880", "adapter-key", self.lights, {"0": group},
            SimpleNamespace(
                refresh_once=lambda **kwargs: self.refreshed.append(True),
                hold_native=lambda slots: self.feedback_calls.append(("hold", set(slots))),
                release_native=lambda slots: self.feedback_calls.append(
                    ("release", set(slots))),
                restore_native=lambda previous: self.feedback_calls.append(
                    ("restore", previous)),
            ),
            sender=lambda states: self.calls.append(states),
            native_sender=lambda roles: self.native.append(roles),
        )

    def v2(self, kind, rid, payload, status=200):
        return self.gateway.after_request("PUT", f"/clip/v2/resource/{kind}/{rid}", status,
                                          payload, {"data": [{"rid": rid, "rtype": kind}]})

    def test_explicit_v2_light_and_group_use_only_existing_selected_roles(self):
        self.v2("light", "light-2", {"on": {"on": True}})
        self.v2("grouped_light", "group-all", {"on": {"on": False}})
        self.assertEqual(self.calls, [{"govee-right": True},
                                      {"govee-left": False, "govee-right": False}])
        self.assertEqual(len(self.refreshed), 2)
        # The room also switches the native Hue lights through their own path.
        self.assertEqual(self.native, [{"strip": False, "hue_left": False}])

    def test_native_hue_lights_switch_through_the_genuine_bridge(self):
        self.v2("light", "light-3", {"on": {"on": True}})
        self.gateway.after_request("PUT", "/api/key/lights/4/state", 200,
                                   {"on": False}, [{"success": {"/lights/4/state/on": False}}])
        self.assertEqual(self.native, [{"strip": True}, {"hue_left": False}])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.feedback_calls, [("hold", {"3"}), ("release", {"3"}),
                                               ("hold", {"4"}), ("release", {"4"})])

    def test_any_enrolled_native_role_gets_power_from_the_bridge_slot_map(self):
        self.gateway = HuePowerRequests(
            "http://127.0.0.1:19880", "adapter-key", self.lights, {}, self.gateway.feedback,
            sender=lambda states: self.calls.append(states),
            native_sender=lambda roles: self.native.append(roles),
            native_slots={"7": "hue_desk"})
        self.v2("light", "light-7", {"on": {"on": True}})
        self.v2("light", "light-3", {"on": {"on": True}})
        self.assertEqual(self.native, [{"hue_desk": True}])

    def test_unconfirmed_native_power_restores_the_real_state(self):
        def refuse(roles):
            raise ValueError("Power was not confirmed")

        self.gateway.native_sender = refuse
        with self.assertRaises(HuePowerUnavailable):
            self.gateway.after_request(
                "PUT", "/clip/v2/resource/light/light-3", 200, {"on": {"on": True}},
                {"data": [{"rid": "light-3", "rtype": "light"}]},
                previous={"3": False, "1": True})
        self.assertEqual(self.feedback_calls[-2:], [("release", {"3"}),
                                                     ("restore", {"3": False})])
        refreshed = len(self.refreshed)
        self.gateway.native_sender = lambda roles: self.native.append(roles)
        self.gateway.enabled = lambda: False
        with self.assertRaises(HuePowerUnavailable):
            self.gateway.after_request(
                "PUT", "/clip/v2/resource/light/light-3", 200, {"on": {"on": True}},
                {"data": [{"rid": "light-3", "rtype": "light"}]}, previous={"3": False})
        self.assertEqual(self.native, [])
        # The owner hold also releases the write hold and restores the real state.
        self.assertEqual(self.feedback_calls[-2:], [("release", {"3"}),
                                                     ("restore", {"3": False})])
        self.assertEqual(len(self.refreshed), refreshed + 1)

    def test_explicit_v1_light_and_group_route_the_same_roles(self):
        for resource, rid, field in (("lights", "2", "state"), ("groups", "0", "action")):
            address = f"/{resource}/{rid}/{field}/on"
            self.gateway.after_request("PUT", f"/api/key/{resource}/{rid}/{field}", 200,
                                       {"on": True}, [{"success": {address: True}}])
        self.assertEqual(self.calls, [{"govee-right": True},
                                      {"govee-left": True, "govee-right": True}])

    def test_scenes_colors_internal_or_unselected_resources_never_send_power(self):
        self.v2("scene", "scene-id", {"recall": {"action": "active"}, "on": {"on": True}})
        self.v2("light", "light-2", {"dimming": {"brightness": 50}})
        self.v2("light", "light-7", {"on": {"on": True}})
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
