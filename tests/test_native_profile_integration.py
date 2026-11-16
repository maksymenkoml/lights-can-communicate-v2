"""Exercise explicit native profile injection with synthetic resources and no device IO."""

import copy
import io
import json
import os
import struct
import sys
import types
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from smart_lighting_core import cli
from smart_lighting_core.config import ConfigError, load_config
from smart_lighting_core.native_profile import NativeHueProfile, NativeProfileError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "experiments/hue-bridge"))
import hue_entertainment_relay as relay_module  # noqa: E402
import live_adapter as pilot  # noqa: E402
from hue_entertainment_relay import HueEntertainmentRelay  # noqa: E402
from hue_strip_proxy import HueStripProxy, PinnedHueClient  # noqa: E402
from test_hue_entertainment_relay import FakeConnection  # noqa: E402
from test_live_adapter import targets  # noqa: E402
from test_native_profile import (  # noqa: E402
    AREA,
    CENTER,
    CENTER_SERVICE,
    LEFT,
    OTHER,
    PLAY_IDS,
    RIGHT,
    STRIP,
    STRIP_ID,
    ZERO,
    profile_data,
)


def hue_config():
    # Deliberately stale hints: the explicit profile is the resource authority.
    return {"bridge": "192.168.99.2", "hue_key": "c" * 64, "light": "stale-light-hint"}


def stream_config():
    return {
        "bridge": "192.168.99.2", "hue_key": "d" * 64,
        "client_key": "e" * 32, "area_id": "stale-area-hint",
        "application_id": "synthetic-stream-owner",
    }


def synthetic_area():
    data = profile_data()
    return {
        "id": AREA,
        "status": "inactive",
        "light_services": [{"rid": rid, "rtype": "light"} for rid in (STRIP, LEFT, CENTER, RIGHT)],
        "channels": [
            {"channel_id": item["channel_id"], "members": [
                {"service": {"rid": item["member"]["rid"], "rtype": "entertainment"},
                 "index": item["member"]["index"]}
            ]}
            for item in data["entertainment"]["channels"]
        ],
    }


class ProfileClientIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.profile = NativeHueProfile.from_dict(profile_data())

    def test_explicit_profile_routes_each_light_without_credential_light_hint(self):
        for role, resource in self.profile.light_ids.items():
            calls = []

            def request(method, path, body):
                calls.append((method, path, copy.deepcopy(body)))
                if method == "GET":
                    return {"data": [{"id": resource}]}
                return {"data": [{"rid": resource}], "errors": []}

            config = hue_config()
            config.pop("light")
            client = PinnedHueClient(config, requester=request, resource_id=resource,
                                     profile=self.profile)
            self.assertIs(client.profile, self.profile)
            self.assertEqual(client.read(), {"id": resource})
            body = {"dimming": {"brightness": 25}, "dynamics": {"duration": 400}}
            client.write(body)
            self.assertEqual(calls, [
                ("GET", "/clip/v2/resource/light/" + resource, None),
                ("PUT", "/clip/v2/resource/light/" + resource, body),
            ])
            proxy = HueStripProxy(client, role=role)
            self.assertIs(proxy.profile, self.profile)
            self.assertEqual(proxy.resource_id, resource)
            self.assertEqual(proxy.brightness_scale, 0.5 if role == "hue_center" else 1)
        default = PinnedHueClient(hue_config(), profile=self.profile)
        self.assertEqual(default.resource_id, STRIP)

    def test_explicit_profile_rejects_unknown_area_service_and_unselected_resources(self):
        for resource in (OTHER, AREA, CENTER_SERVICE, ZERO, "", []):
            with self.subTest(resource=resource), self.assertRaises(ValueError):
                PinnedHueClient(hue_config(), resource_id=resource, profile=self.profile)
        client = PinnedHueClient(hue_config(), requester=lambda *_: {"data": [{"id": OTHER}]},
                                 profile=self.profile)
        with self.assertRaises(ValueError):
            client.read()
        client.requester = lambda *_: {"data": [{"rid": OTHER}]}
        with self.assertRaises(ValueError):
            client.write({"dimming": {"brightness": 50}})

    def test_only_profile_objects_are_accepted(self):
        for invalid in (profile_data(), {}, [], False, 0, "private-profile"):
            with self.subTest(value=invalid):
                with self.assertRaises(ValueError):
                    PinnedHueClient(hue_config(), profile=invalid)
                with self.assertRaises(ValueError):
                    HueStripProxy(types.SimpleNamespace(resource_id=STRIP), profile=invalid)
                with self.assertRaises(ValueError):
                    HueEntertainmentRelay(stream_config(), profile=invalid)

    def test_proxy_requires_matching_role_and_consistent_client_profile(self):
        client = PinnedHueClient(hue_config(), profile=self.profile)
        self.assertIs(HueStripProxy(client, profile=self.profile).profile, self.profile)
        self.assertIs(HueStripProxy(client).profile, self.profile)
        with self.assertRaises(ValueError):
            HueStripProxy(client, role="hue_left", profile=self.profile)
        other_data = profile_data()
        other_data["lights"]["strip"] = OTHER
        other_profile = NativeHueProfile.from_dict(other_data)
        with self.assertRaises(ValueError):
            HueStripProxy(client, profile=other_profile)
        with self.assertRaises(ValueError):
            HueStripProxy(types.SimpleNamespace(resource_id=STRIP, profile={}))
        with self.assertRaises(ValueError):
            HueStripProxy(types.SimpleNamespace(resource_id=OTHER), profile=self.profile)

    def test_credential_light_hints_cannot_authorize_output_without_profile(self):
        config = {**hue_config(), "light": STRIP_ID}
        for resource in (None, STRIP_ID, *PLAY_IDS.values()):
            with self.subTest(resource=resource), self.assertRaises(ValueError):
                PinnedHueClient(config, resource_id=resource)
        with self.assertRaises(ValueError):
            HueStripProxy(types.SimpleNamespace(resource_id=STRIP_ID))


class ProfileRelayIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.profile = NativeHueProfile.from_dict(profile_data())
        self.area = synthetic_area()
        self.calls = []
        self.connection = FakeConnection()
        self.relay = HueEntertainmentRelay(
            stream_config(), requester=self.request,
            connection_factory=lambda _: self.connection, profile=self.profile,
        )

    def request(self, method, body=None):
        self.calls.append((method, copy.deepcopy(body)))
        if method == "GET":
            return copy.deepcopy(self.area)
        if body == {"action": "start"}:
            self.area.update(status="active", active_streamer={"rid": "synthetic-stream-owner"})
        elif body == {"action": "stop"}:
            self.area.update(status="inactive")
            self.area.pop("active_streamer", None)
        return {"errors": []}

    def test_explicit_area_start_frame_and_stop_preserve_mapping_and_power_rules(self):
        self.area["channels"].reverse()
        self.assertIs(self.relay.profile, self.profile)
        self.assertTrue(self.relay.start())
        self.relay.send({i: (1000 + i, 2000 + i, 3000 + i) for i in range(14)})
        packet = self.connection.frames[0]
        self.assertEqual(packet[:16], b"HueStream\x02\x00\x00\x00\x00\x00\x00")
        self.assertEqual(packet[16:52], AREA.encode("ascii"))
        self.assertEqual(len(packet), 136)
        rows = [struct.unpack("!BHHH", packet[i:i + 7]) for i in range(52, len(packet), 7)]
        self.assertEqual(rows[:4], [
            (0, 506, 1006, 1506), (1, 1011, 2011, 3011),
            (2, 1013, 2013, 3013), (3, 1002, 2002, 3002),
        ])
        self.assertEqual(rows[-1], (11, 1010, 2010, 3010))
        self.relay.stop()
        self.assertTrue(self.connection.closed)
        self.assertEqual([body for method, body in self.calls if method == "PUT"],
                         [{"action": "start"}, {"action": "stop"}])

    def test_request_uses_instance_area_url_without_opening_http(self):
        requests = []

        def open_request(request, **_):
            requests.append((request.full_url, request.get_method(), request.data))
            return io.BytesIO(json.dumps({"data": [self.area], "errors": []}).encode())

        opener = types.SimpleNamespace(open=open_request)
        with patch.object(relay_module.urllib.request, "build_opener", return_value=opener):
            self.assertEqual(self.relay.request("GET"), self.area)
            self.relay.request("PUT", {"action": "start"})
        expected_url = "https://192.168.99.2/clip/v2/resource/entertainment_configuration/" + AREA
        self.assertEqual(requests, [(expected_url, "GET", None),
                                    (expected_url, "PUT", b'{"action": "start"}')])

    def test_changed_or_malformed_area_fails_before_start_or_dtls(self):
        invalid = [None, [], {**self.area, "id": OTHER},
                   {**self.area, "status": "unknown"},
                   {**self.area, "light_services": [None] * 4},
                   {**self.area, "channels": [None] * 12}]
        for path, value in [
            (("light_services", 0, "rid"), OTHER),
            (("light_services", 0, "rtype"), "device"),
            (("light_services", 0, "rtype"), None),
            (("channels", 0, "channel_id"), True),
            (("channels", 1, "channel_id"), 0),
            (("channels", 0, "members", 0, "index"), True),
            (("channels", 0, "members", 0, "index"), 2),
            (("channels", 0, "members", 0, "service", "rid"), OTHER),
            (("channels", 0, "members", 0, "service", "rtype"), "device"),
            (("channels", 0, "members", 0, "service", "rtype"), None),
        ]:
            area = synthetic_area()
            target = area
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
            invalid.append(area)
        for area in invalid:
            calls = []

            def request(method, body=None):
                calls.append((method, body))
                return area

            self.relay.requester = request
            with self.subTest(area=area), self.assertRaises(ValueError):
                self.relay.start()
            self.assertEqual(calls, [("GET", None)])
            self.assertFalse(self.relay.owned)
            self.assertIsNone(self.relay.connection)
        self.assertEqual(self.connection.frames, [])

    def test_profile_does_not_bypass_stream_credentials_or_bridge_validation(self):
        for changes in ({"client_key": "invalid"}, {"bridge": "public.example"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                HueEntertainmentRelay({**stream_config(), **changes}, profile=self.profile)
        with self.assertRaises(ValueError):
            HueEntertainmentRelay(stream_config())


class ProfileConfigurationIntegrationTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        self.temp = TemporaryDirectory(prefix="tmp_native_profile_integration_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.config_path = self.root / "service.json"
        self.profile_path = self.root / "native.json"
        self.profile_path.write_text(json.dumps(profile_data()))
        (self.root / "targets.json").write_text(json.dumps({"targets": targets()}))
        (self.root / "hue.json").write_text(json.dumps(hue_config()))
        (self.root / "stream.json").write_text(json.dumps(stream_config()))
        for name, token in (("token", "a" * 48), ("power-token", "b" * 64)):
            path = self.root / name
            path.write_text(token)
            path.chmod(0o600)
        self.data = {
            "version": 1, "state_dir": "state",
            "adapter": {
                "targets_file": "targets.json", "token_file": "token",
                "power_owner_token_file": "power-token", "hue_config_file": "hue.json",
                "hue_play": True, "hue_stream_config_file": "stream.json",
                "native_profile_file": "native.json",
            },
        }

    def config(self):
        self.config_path.write_text(json.dumps(self.data))
        return load_config(self.config_path)

    def no_device_io(self, stack):
        for target in ("hue_strip_proxy.PinnedHueClient.request",
                       "hue_entertainment_relay.HueEntertainmentRelay.request",
                       "hue_entertainment_relay.DtlsConnection.__init__",
                       "live_adapter.threading.Thread.start", "live_adapter.lan.send_to_target"):
            stack.enter_context(patch(target, side_effect=AssertionError("Unexpected device IO")))
        stack.enter_context(patch.object(cli.shutil, "which", return_value="/synthetic/openssl"))
        stack.enter_context(patch.object(cli.subprocess, "run", return_value=
                                        types.SimpleNamespace(stdout=b"OpenSSL synthetic")))

    def capture_constructors(self, stack):
        objects = {"PinnedHueClient": [], "HueStripProxy": [], "HueEntertainmentRelay": []}
        for name in objects:
            constructor = getattr(pilot, name)

            def capture(*args, _name=name, _constructor=constructor, **kwargs):
                result = _constructor(*args, **kwargs)
                objects[_name].append(result)
                return result

            stack.enter_context(patch.object(pilot, name, side_effect=capture))
        return objects

    def assert_shared_profile(self, objects):
        self.assertEqual(len(objects["PinnedHueClient"]), 4)
        self.assertEqual(len(objects["HueStripProxy"]), 4)
        self.assertEqual(len(objects["HueEntertainmentRelay"]), 1)
        profile = objects["PinnedHueClient"][0].profile
        self.assertIsInstance(profile, NativeHueProfile)
        self.assertEqual(profile.to_dict(), profile_data())
        for instances in objects.values():
            for instance in instances:
                self.assertIs(instance.profile, profile)
        self.assertEqual([instance.resource_id for instance in objects["PinnedHueClient"]],
                         [STRIP, LEFT, CENTER, RIGHT])

    def test_relative_profile_path_arguments_dependency_and_legacy_config(self):
        config = self.config()
        self.assertEqual(config.native_profile_file, self.profile_path)
        arguments = config.arguments()
        self.assertEqual(arguments[arguments.index("--native-profile") + 1], str(self.profile_path))
        del self.data["adapter"]["hue_config_file"]
        self.data["adapter"]["hue_play"] = False
        del self.data["adapter"]["hue_stream_config_file"]
        with self.assertRaises(ConfigError):
            self.config()
        del self.data["adapter"]["native_profile_file"]
        legacy = self.config()
        self.assertIsNone(legacy.native_profile_file)
        self.assertNotIn("--native-profile", legacy.arguments())

    def test_prepare_loads_once_and_validates_same_profile_without_device_io(self):
        config = self.config()
        with ExitStack() as stack:
            self.no_device_io(stack)
            objects = self.capture_constructors(stack)
            loader = stack.enter_context(patch.object(NativeHueProfile, "load",
                                                      wraps=NativeHueProfile.load))
            self.assertIs(cli.prepare(config), pilot)
            loader.assert_called_once_with(self.profile_path)
        self.assert_shared_profile(objects)
        self.assertFalse(config.state_dir.exists())

    def test_missing_or_invalid_profile_fails_sanitized_before_runtime_validation(self):
        config = self.config()
        for payload in (None, "private-profile-data", '{"version":1,"version":1}'):
            if payload is None:
                self.profile_path.unlink()
            else:
                self.profile_path.write_text(payload)
            with patch.object(cli, "_credential", side_effect=AssertionError("Too late")):
                with self.subTest(payload=payload), self.assertRaises(ConfigError) as caught:
                    cli.prepare(config)
            self.assertNotIn("private", str(caught.exception))
            self.assertNotIn(str(self.profile_path), str(caught.exception))
        self.assertFalse(config.state_dir.exists())

    def test_check_reports_success_without_network_or_state_changes(self):
        config = self.config()
        config.state_dir.mkdir()
        # Preserve existing pairing and calibration byte-for-byte during preflight.
        (config.state_dir / "pairing.json").write_text('{"paired":true}\n')
        (config.state_dir / "desk-layout.json").write_text('{"calibration":0.65}\n')
        before = {path.relative_to(self.root): path.read_bytes()
                  for path in self.root.rglob("*") if path.is_file()}
        output, errors = io.StringIO(), io.StringIO()
        with ExitStack() as stack, redirect_stdout(output), redirect_stderr(errors):
            self.no_device_io(stack)
            result = cli.main(["--config", str(self.config_path), "--check"])
        self.assertEqual(result, 0, errors.getvalue())
        self.assertTrue(json.loads(output.getvalue())["native_video"])
        self.assertEqual(json.loads(output.getvalue())["profile"], "explicit-native")
        self.assertEqual(before, {path.relative_to(self.root): path.read_bytes()
                                 for path in self.root.rglob("*") if path.is_file()})

    def test_main_wires_single_profile_before_any_server_or_worker_starts(self):
        class BeforeServer(Exception):
            pass

        arguments = ["--targets", str(self.root / "targets.json"), "--token-file",
                     str(self.root / "token"), "--state-dir", str(self.root / "state"),
                     "--hue-config", str(self.root / "hue.json"), "--hue-play",
                     "--hue-stream-config", str(self.root / "stream.json"),
                     "--native-profile", str(self.profile_path)]
        previous_lan_config = pilot.lan.CONFIG
        self.addCleanup(setattr, pilot.lan, "CONFIG", previous_lan_config)
        with ExitStack() as stack:
            self.no_device_io(stack)
            objects = self.capture_constructors(stack)
            loader = stack.enter_context(patch.object(NativeHueProfile, "load",
                                                      wraps=NativeHueProfile.load))
            stack.enter_context(patch.object(pilot, "ThreadingHTTPServer",
                                            side_effect=BeforeServer))
            with self.assertRaises(BeforeServer):
                pilot.main(arguments)
            loader.assert_called_once_with(self.profile_path)
        self.assert_shared_profile(objects)

    def test_main_rejects_bad_profile_before_creating_missing_token(self):
        self.profile_path.write_text("private-profile-data")
        token = self.root / "not-created-token"
        arguments = ["--targets", str(self.root / "targets.json"), "--token-file", str(token),
                     "--hue-config", str(self.root / "hue.json"),
                     "--native-profile", str(self.profile_path)]
        with self.assertRaises(NativeProfileError):
            pilot.main(arguments)
        self.assertFalse(token.exists())


if __name__ == "__main__":
    unittest.main()
