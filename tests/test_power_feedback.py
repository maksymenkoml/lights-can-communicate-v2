import copy
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge"))
from power_feedback import PowerFeedback  # noqa: E402


class Lamp:
    def __init__(self, slot):
        self.id_v1 = slot
        self.state = {"on": True, "bri": 180, "xy": [0.3, 0.4]}
        self.dynamics = {"status": "dynamic_palette"}
        self.events = []

    def genStreamEvent(self, value):
        self.events.append(copy.deepcopy(value))

    def setV1State(self, *args):
        raise AssertionError("Feedback must not invoke a power/scene setter")


def status(left=True, right=False, revision=1, remaining=1.5):
    return {"power_owner": {"enforced": True, "lights": {
        side: {"requested_on": value, "revision": revision, "grant_remaining": remaining,
               "color_allowed": value}
        for side, value in (("govee-left", left), ("govee-right", right))
    }}}


class FeedbackTests(unittest.TestCase):
    def setUp(self):
        self.lights = {str(n): Lamp(str(n)) for n in range(1, 8)}
        self.current = status()
        self.now = 100
        self.resumed, self.groups = [], []
        self.feedback = PowerFeedback(
            "http://adapter", "private-token", self.lights, threading.RLock(),
            resume=lambda lamp: self.resumed.append(lamp.id_v1),
            publish_groups=lambda slots: self.groups.append(set(slots)),
            fetcher=lambda: copy.deepcopy(self.current),
            clock=lambda: self.now,
        )

    def native(self, strip=True, left=True, age=0.5):
        self.current = {**status(), "hue_strip": {"native_on": strip, "native_age": age},
                        "hue_play": {"hue_left": {"native_on": left, "native_age": age},
                                     "hue_center": {"native_on": None, "native_age": age}}}

    def test_native_hue_power_mirrors_the_genuine_bridge(self):
        self.native(strip=False, left=True)
        self.feedback.refresh_once()
        self.assertFalse(self.lights["3"].state["on"])
        self.assertEqual(self.lights["3"].events, [{"on": {"on": False}}])
        self.assertTrue(self.lights["4"].state["on"])
        self.assertEqual(self.lights["4"].events, [])
        self.assertTrue(self.lights["5"].state["on"])
        self.assertIn({"3"}, self.groups)

    def test_a_newly_enrolled_native_role_is_mirrored_too(self):
        feedback = PowerFeedback(
            "http://adapter", "private-token", self.lights, threading.RLock(),
            resume=lambda lamp: None, publish_groups=lambda slots: None,
            fetcher=lambda: {"hue_play": {"hue_desk": {"native_on": False, "native_age": 0}}},
            clock=lambda: self.now, native_slots={"7": "hue_desk"})
        feedback.refresh_once()
        self.assertFalse(self.lights["7"].state["on"])

    def test_stale_or_held_native_readings_never_change_virtual_lights(self):
        self.native(strip=False, age=9)
        self.feedback.refresh_once()
        self.assertTrue(self.lights["3"].state["on"])
        self.native(strip=False)
        self.feedback.hold_native({"3"})
        self.feedback.refresh_once()
        self.assertTrue(self.lights["3"].state["on"])
        self.feedback.release_native({"3"})
        self.feedback.refresh_once()
        self.assertFalse(self.lights["3"].state["on"])
        self.feedback.restore_native({"3": True, "1": False})
        self.assertTrue(self.lights["3"].state["on"])
        self.assertTrue(self.lights["1"].state["on"])

    def test_home_off_updates_only_selected_virtual_light_without_physical_setter(self):
        self.assertTrue(self.feedback.refresh_once())
        self.assertFalse(self.lights["2"].state["on"])
        self.assertEqual(self.lights["2"].events, [{"on": {"on": False}}])
        self.assertEqual(self.groups, [{"2"}])
        self.assertTrue(all(self.lights[str(n)].state["on"] for n in range(3, 8)))

    def test_explicit_command_failure_can_restore_fresh_owner_truth_without_new_revision(self):
        self.feedback.refresh_once()
        # The vendor applied a Hue Off locally, but the physical setter failed.
        self.lights["1"].state["on"] = False
        self.resumed.clear()
        self.feedback.refresh_once(force={"1"})
        self.assertTrue(self.lights["1"].state["on"])
        self.assertEqual(self.resumed, ["1"])
        self.assertFalse(self.lights["2"].state["on"])

    def test_rejected_off_restores_last_known_state_when_authority_has_expired(self):
        self.feedback.refresh_once()
        self.current = status(remaining=0)
        self.lights["1"].state["on"] = False
        self.assertFalse(self.feedback.refresh_once(force={"1"}, fallback={"1": True}))
        self.assertTrue(self.lights["1"].state["on"])
        self.assertIsNotNone(self.feedback.last_error)

    def test_failed_refresh_preserves_newer_home_off_instead_of_old_request_snapshot(self):
        self.feedback.refresh_once()
        previous = {"1": True}
        self.current = status(left=False, revision=2)
        self.feedback.refresh_once()
        self.lights["1"].state["on"] = True

        def unavailable():
            raise OSError("Synthetic disconnected owner")

        self.feedback.fetcher = unavailable
        self.assertFalse(self.feedback.refresh_once(force={"1"}, fallback=previous))
        self.assertFalse(self.lights["1"].state["on"])

    def test_failed_command_without_cached_authority_retains_pre_request_model(self):
        def unavailable():
            raise OSError("Synthetic unavailable owner")

        self.feedback.fetcher = unavailable
        self.lights["1"].state["on"] = False
        self.feedback.refresh_once(force={"1"}, fallback={"1": True})
        self.assertTrue(self.lights["1"].state["on"])

    def test_feedback_keeps_animation_and_palette_and_does_not_repeat_events(self):
        before = copy.deepcopy(self.lights["2"].state)
        self.feedback.refresh_once()
        self.feedback.refresh_once()
        self.assertEqual(len(self.lights["2"].events), 1)
        self.assertEqual(self.lights["2"].dynamics["status"], "dynamic_palette")
        self.assertEqual(self.lights["2"].state["xy"], before["xy"])
        self.assertEqual(self.resumed, ["1"])

    def test_home_on_restores_virtual_state_and_resubmits_current_color_once(self):
        self.feedback.refresh_once()
        self.resumed.clear()
        self.current = status(right=True, revision=2)
        self.current["power_owner"]["lights"]["govee-left"]["revision"] = 1
        self.feedback.refresh_once()
        self.feedback.refresh_once()
        self.assertTrue(self.lights["2"].state["on"])
        self.assertEqual(self.lights["2"].events[-1], {"on": {"on": True}})
        self.assertEqual(self.resumed, ["2"])
        self.assertEqual(self.groups[-1], {"2"})

    def test_expired_grants_and_transport_errors_do_not_invent_power_changes(self):
        self.current = status(right=True)
        self.feedback.refresh_once()
        self.current = status(left=False, right=False, remaining=0)
        self.assertFalse(self.feedback.refresh_once())
        self.assertTrue(self.lights["1"].state["on"])
        self.assertTrue(self.lights["2"].state["on"])

        def unavailable():
            raise OSError("unavailable")

        self.feedback.fetcher = unavailable
        self.assertFalse(self.feedback.refresh_once())
        self.assertTrue(self.lights["2"].state["on"])
        self.assertNotIn("private-token", str(self.feedback.snapshot()))

    def test_malformed_authority_is_rejected_before_any_light_change(self):
        for bad in (None, {}, {"enforced": False, "lights": {}},
                    {"enforced": True, "lights": {"govee-left": {"requested_on": False}}}):
            self.current = {"power_owner": bad}
            self.assertFalse(self.feedback.refresh_once())
            self.assertTrue(self.lights["1"].state["on"])
        self.current = status(left=False)
        self.current["power_owner"]["lights"]["govee-right"]["requested_on"] = "false"
        self.assertFalse(self.feedback.refresh_once())
        self.assertTrue(self.lights["1"].state["on"])

    def test_off_masks_scene_on_without_mutating_input_or_unselected_lamps(self):
        self.feedback.refresh_once()
        values = {"on": True, "bri": 200}
        self.assertEqual(self.feedback.constrain(self.lights["2"], values),
                         {"on": False, "bri": 200})
        self.assertTrue(values["on"])
        self.assertEqual(self.feedback.constrain(self.lights["3"], values), values)
        self.assertEqual(self.feedback.constrain(self.lights["1"], values), values)
        self.assertEqual(self.feedback.constrain(self.lights["2"], {"gradient": {}}),
                         {"gradient": {}, "on": False})

    def test_same_on_heartbeat_does_not_undo_hue_off(self):
        self.current = status(right=True)
        self.feedback.refresh_once()
        self.lights["2"].state["on"] = False
        self.feedback.refresh_once()
        self.assertFalse(self.lights["2"].state["on"])

    def test_hue_api_and_events_cannot_advertise_scene_on_while_home_is_off(self):
        self.feedback.refresh_once()
        event = {"on": {"on": True}, "color": {"xy": {"x": 0.3, "y": 0.4}}}
        result = self.feedback.constrain_v2(self.lights["2"], event)
        self.assertFalse(result["on"]["on"])
        self.assertTrue(event["on"]["on"])
        self.assertEqual(result["color"], event["color"])
        self.assertEqual(self.feedback.constrain_v2(self.lights["3"], event), event)
        self.assertEqual(self.feedback.constrain_v2(self.lights["2"], {"gradient": {}}),
                         {"gradient": {}})

    def test_new_revision_after_fast_off_on_refreshes_target_even_if_on_value_unchanged(self):
        self.current = status(right=True)
        self.feedback.refresh_once()
        self.resumed.clear()
        self.current["power_owner"]["lights"]["govee-right"]["revision"] = 3
        self.feedback.refresh_once()
        self.assertEqual(self.resumed, ["2"])

    def test_off_is_reasserted_after_a_scene_overwrites_virtual_state(self):
        self.feedback.refresh_once()
        self.lights["2"].state["on"] = True
        self.feedback.refresh_once()
        self.assertFalse(self.lights["2"].state["on"])

    def test_scene_reply_is_followed_by_off_event_after_client_optimistic_update(self):
        self.feedback.refresh_once()
        self.feedback.after_request("PUT", "/clip/v2/resource/scene/scene-id", 200,
                                    {"recall": {"action": "active"}})
        self.feedback.refresh_once()
        self.assertEqual(len(self.lights["2"].events), 1)
        # The client applies the scene's stored On after the immediate SSE event.
        # A later event must correct that cache even though server state is Off.
        self.now += 1.1
        self.feedback.refresh_once()
        self.assertEqual(self.lights["2"].events, [{"on": {"on": False}}] * 2)
        self.assertEqual(self.groups, [{"2"}, {"2"}])
        self.feedback.refresh_once()
        self.assertEqual(len(self.lights["2"].events), 2)

    def test_deferred_off_never_overrides_a_new_home_on(self):
        self.feedback.refresh_once()
        self.feedback.after_request("PUT", "/clip/v2/resource/light/selected", 200,
                                    {"on": {"on": True}})
        self.current["power_owner"]["lights"]["govee-right"].update(requested_on=True, revision=2)
        self.now += 1.1
        self.feedback.refresh_once()
        self.assertTrue(self.lights["2"].events[-1]["on"]["on"])

    def test_rapid_scene_recalls_correct_after_the_latest_reply(self):
        self.feedback.refresh_once()
        for delay in (0, 0.9):
            self.now += delay
            self.feedback.after_request("PUT", "/clip/v2/resource/scene/id", 200,
                                        {"recall": {"action": "active"}})
        self.now += 0.2
        self.feedback.refresh_once()
        self.assertEqual(len(self.lights["2"].events), 1)
        self.now += 0.9
        self.feedback.refresh_once()
        self.assertEqual(len(self.lights["2"].events), 2)

    def test_only_successful_power_changing_hue_writes_queue_client_correction(self):
        self.feedback.refresh_once()
        for method, path, code, payload in (
            ("GET", "/clip/v2/resource/scene/id", 200, {"recall": {}}),
            ("PUT", "/clip/v2/resource/scene/id", 403, {"recall": {}}),
            ("PUT", "/clip/v2/resource/scene/id", 200, {"speed": 0.5}),
            ("PUT", "/other", 200, {"on": {"on": True}}),
            ("PUT", "/clip/v2/resource/light/id", 200, {"on": "invalid"}),
        ):
            self.feedback.after_request(method, path, code, payload)
        self.now += 2
        self.feedback.refresh_once()
        self.assertEqual(len(self.lights["2"].events), 1)


if __name__ == "__main__":
    unittest.main()
