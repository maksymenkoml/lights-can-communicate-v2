import copy
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "experiments" / "hue-bridge"))
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
    return {"power_owner": {"enforced": True, "roles": {
        side: {"requested_on": value, "revision": revision, "grant_remaining": remaining,
               "color_allowed": value}
        for side, value in (("left", left), ("right", right))
    }}}


class FeedbackTests(unittest.TestCase):
    def setUp(self):
        self.lights = {str(n): Lamp(str(n)) for n in range(1, 8)}
        self.current = status()
        self.resumed, self.groups = [], []
        self.feedback = PowerFeedback(
            "http://adapter", "private-token", self.lights, threading.RLock(),
            resume=lambda lamp: self.resumed.append(lamp.id_v1),
            publish_groups=lambda slots: self.groups.append(set(slots)),
            fetcher=lambda: copy.deepcopy(self.current),
        )

    def test_home_off_updates_only_selected_virtual_light_without_physical_setter(self):
        self.assertTrue(self.feedback.refresh_once())
        self.assertFalse(self.lights["2"].state["on"])
        self.assertEqual(self.lights["2"].events, [{"on": {"on": False}}])
        self.assertEqual(self.groups, [{"2"}])
        self.assertTrue(all(self.lights[str(n)].state["on"] for n in range(3, 8)))

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
        self.current["power_owner"]["roles"]["left"]["revision"] = 1
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
        self.feedback.refresh_once()
        self.assertTrue(self.lights["1"].state["on"])
        self.assertTrue(self.lights["2"].state["on"])

        def unavailable():
            raise OSError("unavailable")

        self.feedback.fetcher = unavailable
        self.assertFalse(self.feedback.refresh_once())
        self.assertTrue(self.lights["2"].state["on"])
        self.assertNotIn("private-token", str(self.feedback.snapshot()))

    def test_malformed_authority_is_rejected_before_any_light_change(self):
        for bad in (None, {}, {"enforced": False, "roles": {}},
                    {"enforced": True, "roles": {"left": {"requested_on": False}}}):
            self.current = {"power_owner": bad}
            self.assertFalse(self.feedback.refresh_once())
            self.assertTrue(self.lights["1"].state["on"])
        self.current = status(left=False)
        self.current["power_owner"]["roles"]["right"]["requested_on"] = "false"
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
        self.current["power_owner"]["roles"]["right"]["revision"] = 3
        self.feedback.refresh_once()
        self.assertEqual(self.resumed, ["2"])

    def test_off_is_reasserted_after_a_scene_overwrites_virtual_state(self):
        self.feedback.refresh_once()
        self.lights["2"].state["on"] = True
        self.feedback.refresh_once()
        self.assertFalse(self.lights["2"].state["on"])


if __name__ == "__main__":
    unittest.main()
