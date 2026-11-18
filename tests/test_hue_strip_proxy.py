import copy
import http.client
import json
import sys
import unittest
from pathlib import Path

from lights_can_communicate_v2_core.native_profile import NativeHueProfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "experiments" / "hue-bridge"))
from hue_strip_proxy import HueStripProxy, PinnedHueClient, hue_payload  # noqa: E402
from test_native_profile import PLAY_IDS, STRIP_ID, profile_data  # noqa: E402


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
        self.profile = NativeHueProfile.from_dict(profile_data())
        self.resource_id = STRIP_ID
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
    def test_scene_gate_keeps_pending_target_until_video_releases_output(self):
        self.proxy.write_gate = lambda: False
        self.proxy.submit(command())
        self.assertFalse(self.proxy.process_once())
        self.assertEqual(self.client.writes, [])
        self.proxy.write_gate = lambda: True
        self.assertTrue(self.proxy.process_once())
        self.assertEqual(len(self.client.writes), 1)

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
        client = PinnedHueClient(config, requester=request, resource_id=selected,
                                 profile=NativeHueProfile.from_dict(profile_data()))
        client.read()
        client.write({"color": {"xy": {"x": 0.2, "y": 0.3}}})
        self.assertEqual({item[1] for item in requests}, {"/clip/v2/resource/light/" + selected})
        with self.assertRaises(ValueError):
            PinnedHueClient(config, resource_id="unselected-light",
                            profile=NativeHueProfile.from_dict(profile_data()))

    def test_native_power_is_a_separate_confirmed_write_for_the_pinned_light(self):
        requests, state = [], {"on": False}
        selected = PLAY_IDS["hue_left"]

        def request(method, path, body):
            requests.append((method, path, body))
            if method == "PUT" and "on" in body:
                state["on"] = body["on"]["on"]
            return {"data": [{"id": selected, "rid": selected, "on": dict(state)}],
                    "errors": []}

        client = PinnedHueClient({"bridge": "192.168.10.21", "hue_key": "test"},
                                 requester=request, resource_id=selected,
                                 profile=NativeHueProfile.from_dict(profile_data()))
        proxy = HueStripProxy(client, clock=lambda: 10, wall_clock=lambda: 1000,
                              role="hue_left")
        self.assertTrue(proxy.set_power(True, sleep=lambda _: None))
        self.assertEqual(requests[0], ("PUT", "/clip/v2/resource/light/" + selected,
                                       {"on": {"on": True}}))
        self.assertTrue(proxy.snapshot()["native_on"])
        self.assertTrue(proxy.restore)
        # The color path still refuses power, and only booleans switch power.
        with self.assertRaises(ValueError):
            client.write({"on": {"on": False}})
        with self.assertRaises(ValueError):
            client.power("off")

    def test_native_power_that_does_not_read_back_is_not_confirmed(self):
        client = FakeHue()
        client.power = lambda on: None
        proxy = HueStripProxy(client, clock=lambda: 10, wall_clock=lambda: 1000)
        self.assertFalse(proxy.set_power(False, attempts=3, sleep=lambda _: None))
        # Status still reports the light's actual power from the read-back.
        self.assertTrue(proxy.snapshot()["native_on"])

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
            {"bridge": "192.168.10.21", "hue_key": "test", "light": STRIP_ID}, requester=request,
            profile=NativeHueProfile.from_dict(profile_data()),
        )
        client.read()
        client.write(hue_payload(state(), 47))
        self.assertEqual({item[1] for item in requests}, {"/clip/v2/resource/light/" + STRIP_ID})
        with self.assertRaises(ValueError):
            client.write({"on": {"on": True}})


class FakeNames:
    def __init__(self, names):
        self.value = names

    def names(self):
        return dict(self.value)

    def scale(self, role, fallback):
        return fallback


class OwnerNameTests(unittest.TestCase):
    def pinned(self, light_name="Hue Monitor"):
        requests = []
        light = {"id": STRIP_ID, "metadata": {"name": light_name}, "on": {"on": True}}

        def request(method, path, body):
            requests.append((method, path, body))
            if method == "PUT" and "metadata" in body:
                light["metadata"] = dict(body["metadata"])
            return {"data": [dict(light, rid=STRIP_ID)], "errors": []}

        client = PinnedHueClient(
            {"bridge": "192.168.10.21", "hue_key": "test"}, requester=request,
            profile=NativeHueProfile.from_dict(profile_data()))
        return client, requests

    def test_rename_sends_only_the_name_and_refuses_unusable_names(self):
        client, requests = self.pinned()
        client.rename("Desk strip")
        self.assertEqual(requests, [("PUT", "/clip/v2/resource/light/" + STRIP_ID,
                                     {"metadata": {"name": "Desk strip"}})])
        for name in ("", "x" * 33, 5, None):
            with self.assertRaises(ValueError):
                client.rename(name)
        client.rename("x" * 32)
        # The color path still has no metadata or name access.
        with self.assertRaises(ValueError):
            client.write({"metadata": {"name": "Other"}})
        self.assertEqual(len(requests), 2)

    def test_the_name_is_written_once_per_layout_change(self):
        client, requests = self.pinned()
        store = FakeNames({})
        proxy = HueStripProxy(client, layout_store=store, clock=lambda: 10)
        writes = lambda: [r for r in requests if r[0] == "PUT"]  # noqa: E731
        # No owner name: nothing is read or written, however often it runs.
        self.assertFalse(proxy.sync_owner_name())
        self.assertEqual(requests, [])
        store.value = {"strip": "Desk strip"}
        self.assertTrue(proxy.sync_owner_name())
        self.assertEqual([r[2] for r in writes()], [{"metadata": {"name": "Desk strip"}}])
        count = len(requests)
        for _ in range(3):
            self.assertFalse(proxy.sync_owner_name())
        self.assertEqual(len(requests), count)
        # Another role's name never reaches this light.
        store.value = {"strip": "Desk strip", "hue_left": "Left"}
        self.assertFalse(proxy.sync_owner_name())
        self.assertEqual(len(requests), count)
        store.value = {"strip": "Monitor bar"}
        self.assertTrue(proxy.sync_owner_name())
        self.assertEqual(writes()[-1][2], {"metadata": {"name": "Monitor bar"}})
        # Removing the owner name leaves the light's name alone.
        store.value = {}
        self.assertFalse(proxy.sync_owner_name())
        self.assertEqual(len(writes()), 2)

    def test_a_light_that_already_has_the_name_is_not_written(self):
        client, requests = self.pinned(light_name="Desk strip")
        proxy = HueStripProxy(client, layout_store=FakeNames({"strip": "Desk strip"}),
                              clock=lambda: 10)
        self.assertFalse(proxy.sync_owner_name())
        self.assertEqual([r[0] for r in requests], ["GET"])
        self.assertFalse(proxy.sync_owner_name())
        self.assertEqual(len(requests), 1)

    def test_a_name_set_in_the_hue_app_stays_until_the_layout_name_changes(self):
        client, requests = self.pinned()
        store = FakeNames({"strip": "Desk strip"})
        proxy = HueStripProxy(client, layout_store=store, clock=lambda: 10)
        proxy.sync_owner_name()
        client.rename("Chosen in Hue app")
        count = len(requests)
        self.assertFalse(proxy.sync_owner_name())
        self.assertEqual(len(requests), count)

    def test_a_failed_name_write_is_retried_only_after_the_pause(self):
        clock = [10.0]
        calls = []

        class Broken(FakeHue):
            def read(self):
                calls.append("read")
                raise OSError("bridge unreachable")

        proxy = HueStripProxy(Broken(), layout_store=FakeNames({"strip": "Desk strip"}),
                              clock=lambda: clock[0])
        self.assertFalse(proxy.sync_owner_name())
        self.assertFalse(proxy.sync_owner_name())
        self.assertEqual(calls, ["read"])
        clock[0] += 31
        proxy.sync_owner_name()
        self.assertEqual(calls, ["read", "read"])


class FakeResponse:
    def __init__(self, status, body, will_close=False):
        self.status, self.body, self.will_close = status, body, will_close

    def read(self):
        return json.dumps(self.body).encode()


class FakeConnection:
    """One bridge connection; ``failures`` raise on the next requests in turn."""

    def __init__(self, log, failures=(), responses=()):
        self.log, self.failures, self.responses = log, list(failures), list(responses)
        self.closed = False
        self.pending = None

    def request(self, method, path, body=None, headers=None):
        if self.failures:
            raise self.failures.pop(0)
        self.log.append((self, method, path, None if body is None else json.loads(body),
                         headers["hue-application-key"]))
        self.pending = self.responses.pop(0) if self.responses else FakeResponse(
            200, {"data": [{"id": STRIP_ID, "rid": STRIP_ID}], "errors": []})

    def getresponse(self):
        return self.pending

    def close(self):
        self.closed = True


class KeptAliveClientTests(unittest.TestCase):
    def client(self, *connections):
        made = []

        def factory():
            made.append(connections[len(made)])
            return made[-1]

        client = PinnedHueClient(
            {"bridge": "192.168.10.21", "hue_key": "test", "light": STRIP_ID},
            profile=NativeHueProfile.from_dict(profile_data()), connection_factory=factory)
        return client, made

    def test_requests_reuse_one_connection(self):
        log = []
        client, made = self.client(FakeConnection(log))
        client.read()
        client.power(True)
        client.read()
        self.assertEqual(len(made), 1)
        self.assertEqual([entry[1:4] for entry in log], [
            ("GET", "/clip/v2/resource/light/" + STRIP_ID, None),
            ("PUT", "/clip/v2/resource/light/" + STRIP_ID, {"on": {"on": True}}),
            ("GET", "/clip/v2/resource/light/" + STRIP_ID, None),
        ])
        self.assertEqual({entry[4] for entry in log}, {"test"})

    def test_a_connection_the_bridge_closed_while_idle_is_replaced_once(self):
        log = []
        stale = FakeConnection(log, failures=[http.client.RemoteDisconnected("idle")])
        fresh = FakeConnection(log)
        client, made = self.client(fresh)
        client.connection = stale
        client.power(False)
        self.assertTrue(stale.closed)
        self.assertEqual(made, [fresh])
        self.assertEqual([entry[0] for entry in log], [fresh])

    def test_a_new_connection_that_fails_is_not_retried(self):
        failing = FakeConnection([], failures=[ConnectionResetError("reset")])
        client, made = self.client(failing)
        with self.assertRaises(ConnectionResetError):
            client.read()
        self.assertTrue(failing.closed)
        self.assertIsNone(client.connection)
        self.assertEqual(len(made), 1)

    def test_a_timeout_drops_the_connection_without_sending_again(self):
        log = []
        slow = FakeConnection(log, failures=[TimeoutError("timed out")])
        client, made = self.client(FakeConnection(log))
        client.connection = slow
        with self.assertRaises(TimeoutError):
            client.power(True)
        self.assertTrue(slow.closed)
        self.assertIsNone(client.connection)
        self.assertEqual((log, made), ([], []))

    def test_http_errors_and_bridge_close_requests_are_honoured(self):
        log = []
        connection = FakeConnection(log, responses=[
            FakeResponse(403, {"errors": [{"description": "unauthorized"}]}),
            FakeResponse(200, {"data": [{"id": STRIP_ID}], "errors": []}, will_close=True),
        ])
        client, made = self.client(connection)
        with self.assertRaises(OSError):
            client.read()
        self.assertIs(client.connection, connection)
        client.read()
        self.assertTrue(connection.closed)
        self.assertIsNone(client.connection)

    def test_the_pinned_path_is_still_enforced(self):
        client, made = self.client(FakeConnection([]))
        with self.assertRaises(ValueError):
            client.request("GET", "/clip/v2/resource/light/other", None)
        self.assertEqual(made, [])


if __name__ == "__main__":
    unittest.main()
