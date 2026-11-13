import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "experiments" / "hue-bridge"))
from hue_strip_proxy import PLAY_IDS, STRIP_ID, HueStripProxy, PinnedHueClient, hue_payload  # noqa: E402


def state():
    colors = (
        {"x": 0.1783, "y": 0.1212},
        {"x": 0.2299, "y": 0.0862},
        {"x": 0.4039, "y": 0.1914},
        {"x": 0.5402, "y": 0.4218},
        {"x": 0.474, "y": 0.4378},
    )
    return {
        "on": True,
        "bri": 198,
        "colormode": "xy",
        "gradient": {
            "points": [{"color": {"xy": xy}, "dimming": {"brightness": 78}} for xy in colors]
        },
        "_changed": ["on", "bri", "gradient"],
    }


def command(sequence=1, **values):
    return {
        "side": "strip",
        "state": state(),
        "transitiontime": 47,
        "session": "scene-session",
        "sequence": sequence,
        "issued_at": 1000.0,
        **values,
    }


class FakeHue:
    def __init__(self):
        self.native = {
            "id": STRIP_ID,
            "mode": "normal",
            "on": {"on": True},
            "gradient": {"points_capable": 5, "points": []},
            "dimming": {"brightness": 40},
        }
        self.writes = []

    def read(self):
        return copy.deepcopy(self.native)

    def write(self, body):
        self.writes.append(copy.deepcopy(body))


class HueStripTests(unittest.TestCase):
    def test_center_play_halves_each_scene_brightness_and_restores_without_double_scaling(self):
        client = FakeHue()
        client.resource_id = PLAY_IDS["hue_center"]
        client.native["id"] = client.resource_id
        proxy = HueStripProxy(client, clock=lambda: 10, wall_clock=lambda: 1000, role="hue_center")
        value = {
            "on": True,
            "bri": 254,
            "colormode": "xy",
            "xy": [0.2, 0.3],
            "_changed": ["on", "bri", "xy"],
        }
        proxy.submit(command(side="hue_center", state=value))
        self.assertTrue(proxy.process_once())
        self.assertEqual(client.writes[-1]["dimming"]["brightness"], 50)
        self.assertEqual(client.writes[-1]["color"]["xy"], {"x": 0.2, "y": 0.3})
        self.assertEqual(client.writes[-1]["dynamics"]["duration"], 4700)
        value.update({"bri": 127, "_changed": ["bri"]})
        proxy.submit(command(sequence=2, side="hue_center", state=value))
        self.assertTrue(proxy.process_once())
        self.assertEqual(client.writes[-1]["dimming"]["brightness"], 25)
        self.assertNotIn("color", client.writes[-1])
        client.native["on"]["on"] = False
        self.assertFalse(proxy.process_once())
        client.native["on"]["on"] = True
        self.assertTrue(proxy.process_once())
        self.assertEqual(client.writes[-1]["dimming"]["brightness"], 25)
        self.assertNotIn("on", client.writes[-1])

    def test_strip_keeps_full_scene_brightness(self):
        self.proxy.submit(command(state={"on": True, "bri": 254, "_changed": ["bri"]}))
        self.assertTrue(self.proxy.process_once())
        self.assertEqual(self.client.writes[-1]["dimming"]["brightness"], 100)

    def test_confirmed_play_roles_pin_their_own_ids_without_allowing_other_hue_devices(self):
        requests = []
        selected = PLAY_IDS["hue_left"]

        def request(method, path, body):
            requests.append((method, path, body))
            return {"data": [{"id": selected, "rid": selected}], "errors": []}

        config = {"bridge": "192.168.10.21", "hue_key": "test", "light": STRIP_ID}
        client = PinnedHueClient(config, requester=request, resource_id=selected)
        client.read()
        client.write({"color": {"xy": {"x": 0.2, "y": 0.3}}})
        self.assertEqual({item[1] for item in requests}, {"/clip/v2/resource/light/" + selected})
        with self.assertRaises(ValueError):
            PinnedHueClient(config, resource_id="unselected-light")

    def test_play_proxy_accepts_only_its_configured_role(self):
        client = FakeHue()
        client.resource_id = PLAY_IDS["hue_left"]
        client.native["id"] = client.resource_id
        client.native.pop("gradient")
        proxy = HueStripProxy(client, clock=lambda: 10, wall_clock=lambda: 1000, role="hue_left")
        value = {
            "on": True,
            "bri": 198,
            "colormode": "xy",
            "xy": [0.2, 0.3],
            "_changed": ["on", "bri", "xy"],
        }
        proxy.submit(command(side="hue_left", state=value))
        self.assertTrue(proxy.process_once())
        self.assertNotIn("on", client.writes[-1])
        with self.assertRaises(ValueError):
            proxy.submit(command(sequence=2, side="hue_right", state=value))

    def setUp(self):
        self.client = FakeHue()
        self.now = 10.0
        self.wall = 1000.0
        self.proxy = HueStripProxy(
            self.client, clock=lambda: self.now, wall_clock=lambda: self.wall
        )

    def test_gradient_points_and_duration_preserved_without_power_fields(self):
        body = hue_payload(state(), 47)
        self.assertEqual(len(body["gradient"]["points"]), 5)
        self.assertEqual(set(body), {"gradient", "dimming", "dynamics"})
        self.assertEqual(body["dynamics"]["duration"], 4700)
        self.assertAlmostEqual(body["dimming"]["brightness"], 198 / 254 * 100)
        self.assertTrue(all(set(point) == {"color"} for point in body["gradient"]["points"]))

    def test_brightness_only_does_not_reapply_stale_gradient(self):
        value = state()
        value["_changed"] = ["bri"]
        self.assertEqual(set(hue_payload(value, 4)), {"dimming", "dynamics"})

    def test_dynamic_gradient_uses_its_palette_brightness_without_a_static_recall(self):
        value = state()
        value["_changed"] = ["gradient"]
        value["bri"] = 254
        self.assertEqual(hue_payload(value, 47)["dimming"]["brightness"], 78)

    def test_white_temperature_does_not_reapply_stale_gradient(self):
        value = state()
        value.update({"colormode": "ct", "ct": 250, "_changed": ["ct"]})
        body = hue_payload(value, 4)
        self.assertNotIn("gradient", body)
        self.assertEqual(body["color_temperature"], {"mirek": 250})

    def test_native_off_is_not_turned_on_and_restore_uses_accepted_target(self):
        self.client.native["on"]["on"] = False
        self.proxy.submit(command())
        self.assertFalse(self.proxy.process_once())
        self.assertEqual(self.client.writes, [])
        self.wall += 20
        self.now += 20
        self.client.native["on"]["on"] = True
        self.assertTrue(self.proxy.process_once())
        self.assertNotIn("on", self.client.writes[-1])

    def test_streaming_blocks_strip_and_shared_govee_output(self):
        self.client.native["mode"] = "streaming"
        self.proxy.submit(command())
        self.assertFalse(self.proxy.process_once())
        self.assertFalse(self.proxy.govee_allowed())
        self.assertEqual(self.client.writes, [])

    def test_stale_native_snapshot_blocks_shared_output(self):
        self.proxy.process_once()
        self.assertTrue(self.proxy.govee_allowed())
        self.now += 3
        self.assertFalse(self.proxy.govee_allowed())

    def test_normal_command_is_sent_once_and_cannot_change_resource(self):
        self.proxy.submit(command())
        self.assertTrue(self.proxy.process_once())
        self.assertFalse(self.proxy.process_once())
        self.assertEqual(len(self.client.writes), 1)
        self.assertNotIn("on", self.client.writes[0])
        with self.assertRaises(ValueError):
            self.proxy.submit(command(sequence=2, side="third"))

    def test_wrong_native_resource_fails_closed(self):
        self.client.native["id"] = "another-light"
        self.proxy.submit(command())
        self.assertFalse(self.proxy.process_once())
        self.assertEqual(self.client.writes, [])
        self.assertFalse(self.proxy.govee_allowed())

    def test_replay_old_command_and_virtual_stop(self):
        self.assertTrue(self.proxy.submit(command()))
        self.assertFalse(self.proxy.submit(command()))
        self.assertTrue(self.proxy.submit(command(sequence=2, state={"on": False})))
        self.assertFalse(self.proxy.process_once())
        self.assertEqual(self.client.writes, [])

    def test_pending_gradient_survives_brightness_delta_while_native_off(self):
        self.client.native["on"]["on"] = False
        self.proxy.submit(command())
        update = state()
        update.update({"bri": 101, "_changed": ["bri"]})
        self.proxy.submit(command(sequence=2, state=update, transitiontime=4))
        self.proxy.process_once()
        self.client.native["on"]["on"] = True
        self.proxy.process_once()
        self.assertEqual(len(self.client.writes[-1]["gradient"]["points"]), 5)
        self.assertAlmostEqual(self.client.writes[-1]["dimming"]["brightness"], 101 / 254 * 100)

    def test_virtual_on_only_restores_full_color_without_a_power_write(self):
        self.proxy.submit(command())
        self.proxy.submit(command(sequence=2, state={"on": False}))
        value = state()
        value.update({"_changed": ["on"], "_color_kind": "gradient"})
        self.proxy.submit(command(sequence=3, state=value))
        self.assertTrue(self.proxy.process_once())
        self.assertIn("gradient", self.client.writes[-1])
        self.assertNotIn("on", self.client.writes[-1])

    def test_virtual_on_with_brightness_or_transition_metadata_restores_color(self):
        for changed in (["on", "transitiontime"], ["on", "bri"], ["on", "bri", "transitiontime"]):
            with self.subTest(changed=changed):
                client = FakeHue()
                proxy = HueStripProxy(client, clock=lambda: 10, wall_clock=lambda: 1000)
                proxy.submit(command(state={"on": False}))
                value = state()
                value.update({"_changed": changed, "_color_kind": "gradient"})
                proxy.submit(command(sequence=2, state=value))
                self.assertTrue(proxy.process_once())
                self.assertIn("gradient", client.writes[-1])
                self.assertNotIn("on", client.writes[-1])

    def test_new_white_mode_replaces_stored_gradient(self):
        self.proxy.submit(command())
        value = state()
        value.update({"colormode": "ct", "ct": 250, "_changed": ["ct"]})
        self.proxy.submit(command(sequence=2, state=value))
        self.proxy.process_once()
        self.assertNotIn("gradient", self.client.writes[-1])
        self.assertEqual(self.client.writes[-1]["color_temperature"], {"mirek": 250})

    def test_brightness_delta_after_applied_gradient_writes_only_dimming(self):
        self.proxy.submit(command())
        self.proxy.process_once()
        value = state()
        value.update({"bri": 101, "_changed": ["bri"]})
        self.proxy.submit(command(sequence=2, state=value))
        self.proxy.process_once()
        self.assertEqual(set(self.client.writes[-1]), {"dimming", "dynamics"})

    def test_pinned_client_rejects_other_resource_and_power_write(self):
        with self.assertRaises(ValueError):
            PinnedHueClient({"bridge": "192.168.10.21", "hue_key": "test", "light": "other"})
        requests = []

        def request(method, path, body):
            requests.append((method, path, body))
            return {"data": [{"id": STRIP_ID, "rid": STRIP_ID}], "errors": []}

        client = PinnedHueClient(
            {"bridge": "192.168.10.21", "hue_key": "test", "light": STRIP_ID}, requester=request
        )
        client.read()
        client.write(hue_payload(state(), 47))
        self.assertEqual({item[1] for item in requests}, {"/clip/v2/resource/light/" + STRIP_ID})
        with self.assertRaises(ValueError):
            client.write({"on": {"on": True}})


if __name__ == "__main__":
    unittest.main()
