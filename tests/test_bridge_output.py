import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge"))
from bridge_output import BridgeOutput, PlayGuard, desk_palette_index  # noqa: E402


class BridgeOutputTests(unittest.TestCase):
    def test_desk_indices_align_edges_center_and_lower_play_lights(self):
        palette = {"color": [{} for _ in range(5)]}
        self.assertEqual(
            [desk_palette_index(str(slot), palette, 99) for slot in range(1, 7)], [0, 4, 0, 0, 2, 4]
        )
        self.assertEqual(desk_palette_index("2", {"color": [{}, {}]}, 99), 0)

    def test_confirmed_play_slots_are_separate_native_roles(self):
        sent = []
        output = BridgeOutput(
            "http://adapter",
            "token",
            sender=lambda value: sent.append(value),
            clock=lambda: 1000,
            include_strip=True,
            include_hue_play=True,
        )
        for slot in ("4", "5", "6"):
            self.assertTrue(output.submit(slot, {"on": True, "bri": 198}, 4))
        for _ in range(3):
            output.forward_once()
        self.assertEqual([item["light"] for item in sent], ["hue_left", "hue_center", "hue_right"])
        self.assertFalse(output.submit("7", {"on": True}, 4))

    def test_strip_slot_requires_explicit_opt_in_and_is_not_a_third_govee(self):
        sent = []
        output = BridgeOutput(
            "http://adapter",
            "token",
            sender=lambda payload: sent.append(payload),
            clock=lambda: 1000,
            include_strip=True,
        )
        self.assertTrue(output.submit("3", {"on": True, "bri": 198}, 47))
        self.assertFalse(output.submit("4", {"on": True}, 4))
        output.forward_once()
        self.assertEqual(sent[0]["light"], "strip")
        self.assertNotIn("device", sent[0])

    def test_strip_coalescing_keeps_unforwarded_gradient_on_brightness_update(self):
        sent = []
        output = BridgeOutput(
            "http://adapter",
            "token",
            sender=lambda value: sent.append(value),
            clock=lambda: 1000,
            include_strip=True,
        )
        output.submit(
            "3",
            {"on": True, "bri": 198, "gradient": {"points": []}, "_changed": ["gradient", "bri"]},
            47,
        )
        output.submit(
            "3", {"on": True, "bri": 101, "gradient": {"points": []}, "_changed": ["bri"]}, 4
        )
        output.forward_once()
        self.assertIn("gradient", sent[0]["state"]["_changed"])
        self.assertEqual(sent[0]["state"]["bri"], 101)

    def test_strip_coalescing_new_ct_mode_cancels_unforwarded_gradient(self):
        sent = []
        output = BridgeOutput(
            "http://adapter",
            "token",
            sender=lambda value: sent.append(value),
            clock=lambda: 1000,
            include_strip=True,
        )
        output.submit("3", {"on": True, "gradient": {"points": []}, "_changed": ["gradient"]}, 47)
        output.submit(
            "3",
            {
                "on": True,
                "ct": 250,
                "colormode": "ct",
                "gradient": {"points": []},
                "_changed": ["ct"],
            },
            4,
        )
        output.forward_once()
        self.assertNotIn("gradient", sent[0]["state"]["_changed"])
        self.assertIn("ct", sent[0]["state"]["_changed"])

    def test_inflight_strip_gradient_is_not_lost_when_delivery_fails(self):
        sent = []
        output = None

        def sender(value):
            if not sent:
                sent.append(value)
                output.submit(
                    "3",
                    {"on": True, "bri": 101, "gradient": {"points": []}, "_changed": ["bri"]},
                    4,
                )
                raise OSError("simulated delivery failure")
            sent.append(value)

        output = BridgeOutput(
            "http://adapter", "token", sender=sender, clock=lambda: 1000, include_strip=True
        )
        output.submit(
            "3", {"on": True, "bri": 198, "gradient": {"points": []}, "_changed": ["gradient"]}, 47
        )
        self.assertFalse(output.forward_once())
        self.assertTrue(output.forward_once())
        self.assertIn("gradient", sent[-1]["state"]["_changed"])
        self.assertEqual(sent[-1]["state"]["bri"], 101)

    def test_only_two_selected_light_slots_are_forwarded(self):
        sent = []
        output = BridgeOutput(
            "http://adapter",
            "private-token",
            sender=lambda payload: sent.append(payload),
            clock=lambda: 1000,
        )
        self.assertFalse(output.submit("3", {"on": True}, 4))
        self.assertTrue(output.submit("1", {"on": True, "bri": 198, "xy": [0.2, 0.3]}, 47))
        output.forward_once()
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["light"], "govee-left")
        self.assertEqual(sent[0]["transitiontime"], 47)
        self.assertEqual(sent[0]["state"]["bri"], 198)
        self.assertNotIn("ip", sent[0])
        self.assertNotIn("private-token", str(sent[0]))

    def test_latest_command_replaces_queued_older_target(self):
        sent = []
        output = BridgeOutput(
            "http://adapter",
            "token",
            sender=lambda payload: sent.append(payload),
            clock=lambda: 1000,
        )
        output.submit("1", {"on": True, "bri": 10}, 47)
        output.submit("1", {"on": True, "bri": 20}, 4)
        output.forward_once()
        self.assertEqual([item["state"]["bri"] for item in sent], [20])
        self.assertEqual(sent[0]["sequence"], 2)

    def test_payload_is_copied_before_the_scene_mutates_its_palette(self):
        sent = []
        state = {"on": True, "bri": 198, "xy": [0.2, 0.3]}
        output = BridgeOutput(
            "http://adapter",
            "token",
            sender=lambda payload: sent.append(payload),
            clock=lambda: 1000,
        )
        output.submit("1", state, 47)
        state["xy"][0] = 0.7
        output.forward_once()
        self.assertEqual(sent[0]["state"]["xy"], [0.2, 0.3])

    def test_old_dynamic_worker_cannot_resume_after_stop_and_new_play(self):
        guard = PlayGuard()
        seen = []
        first = threading.Event()
        release = threading.Event()

        class Lamp:
            id_v1 = "1"
            dynamics = {"status": "none", "status_values": ["dynamic_palette"], "speed": 1}

            def setV2State(self, state):
                seen.append(state["marker"])

        lamp = Lamp()

        def old_worker(proxy, palette, index):
            proxy.dynamics["status"] = "dynamic_palette"
            proxy.setV2State({"marker": "old-first"})
            first.set()
            release.wait(1)
            if proxy.dynamics["status"] == "dynamic_palette":
                proxy.setV2State({"marker": "old-next"})
            proxy.setV2State({"marker": "stale-unconditional-callback"})

        def new_worker(proxy, palette, index):
            proxy.dynamics["status"] = "dynamic_palette"
            proxy.setV2State({"marker": "new-first"})

        worker = threading.Thread(target=guard.run, args=(lamp, {}, 0, old_worker))
        worker.start()
        self.assertTrue(first.wait(1))
        guard.cancel(lamp)
        guard.run(lamp, {}, 0, new_worker)
        release.set()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(seen, ["old-first", "new-first"])

    def test_each_play_worker_gets_its_own_palette_copy(self):
        guard = PlayGuard()

        class Lamp:
            id_v1 = "1"
            dynamics = {"status": "none"}

        palette = {"color": [{"transitiontime": 4}]}

        def worker(proxy, data, index):
            data["color"][0]["transitiontime"] = 47

        guard.run(Lamp(), palette, 0, worker)
        self.assertEqual(palette["color"][0]["transitiontime"], 4)

    def test_generation_is_reserved_when_thread_is_created_not_when_it_starts(self):
        guard = PlayGuard()
        seen = []
        created = []

        class Lamp:
            id_v1 = "1"
            dynamics = {"status": "none"}

            def dynamicScenePlay(self, palette, index):
                seen.append(palette["marker"])

        lamp = Lamp()

        def constructor(*args, **kwargs):
            created.append(kwargs["target"])

        factory = guard.thread_factory(constructor, Lamp.dynamicScenePlay)
        factory(target=lamp.dynamicScenePlay, args=({"marker": "old-request"}, 0))
        guard.cancel(lamp)
        factory(target=lamp.dynamicScenePlay, args=({"marker": "new-request"}, 0))
        created[1]()
        created[0]()
        self.assertEqual(seen, ["new-request"])


if __name__ == "__main__":
    unittest.main()
