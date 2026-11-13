import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "experiments" / "hue-bridge"))
from bridge_output import BridgeOutput, PlayGuard  # noqa: E402


class BridgeOutputTests(unittest.TestCase):
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
        self.assertEqual(sent[0]["side"], "left")
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
