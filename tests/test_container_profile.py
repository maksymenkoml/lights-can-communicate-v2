"""Container profile: configuration, endpoints and hold, offline only."""

import importlib.util
import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from lights_can_communicate_v2_core import cli, services
from lights_can_communicate_v2_core.config import ConfigError, load_config

spec = importlib.util.spec_from_file_location(
    "setup_studio", Path(__file__).resolve().parents[1] / "studio/server.py"
)
studio = importlib.util.module_from_spec(spec)
spec.loader.exec_module(studio)

TOKEN = "a" * 48


class ContainerProfileTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory(prefix="tmp_container_base_")
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.state = self.base / "state"
        self.state.mkdir()
        (self.base / "config").mkdir()
        (self.state / "targets.json").write_text(json.dumps({"targets": [
            {"side": "left", "device": "AA:AA:AA:AA:AA:AA:AA:AA", "ip": "192.168.99.50"},
            {"side": "right", "device": "BB:BB:BB:BB:BB:BB:BB:BB", "ip": "192.168.99.51"},
        ]}))
        for name, content in (("adapter-token", TOKEN), ("owner-token", "b" * 64)):
            (self.state / name).write_text(content)
            (self.state / name).chmod(0o600)
        self.value = {
            "version": 2,
            "state_dir": "../state",
            "adapter": {"targets_file": "../state/targets.json",
                        "token_file": "../state/adapter-token",
                        "power_owner_token_file": "../state/owner-token", "port": 19880},
            "setup": {"port": 19881},
            "gateway": {"lan_ip": "192.168.99.10", "mac": "02:00:00:00:00:0A",
                        "live_output": True, "hue_strip": True, "hue_play": True,
                        "entertainment": True},
            "container": {
                "studio_hosts": ["192.168.99.5", "DiskStation.local", "diskstation.local"],
            },
        }
        self.config_path = self.base / "config" / "core.json"
        self.save()

    def save(self):
        self.config_path.write_text(json.dumps(self.value))

    def rejected(self, change):
        saved = json.loads(json.dumps(self.value))
        change(self.value)
        self.save()
        try:
            with self.assertRaises(ConfigError) as caught:
                load_config(self.config_path)
            self.assertNotIn("PRIVATE", str(caught.exception))
            return str(caught.exception)
        finally:
            self.value = saved
            self.save()

    # Configuration -------------------------------------------------------

    def test_profile_defaults(self):
        config = load_config(self.config_path)
        container = config.container
        self.assertEqual(container.core_address, "172.30.250.3")
        self.assertEqual(config.bind, "172.30.250.3")
        self.assertEqual(config.setup.bind, "0.0.0.0")
        self.assertEqual(config.gateway.adapter_url, "http://172.30.250.3:19880")
        self.assertEqual(container.studio_hosts, ("192.168.99.5", "diskstation.local"))

    def test_the_container_section_is_optional_and_its_core_address_is_configurable(self):
        del self.value["container"]
        self.save()
        self.assertEqual(load_config(self.config_path).container.studio_hosts, ())
        self.value["container"] = {"core_address": "172.30.251.7"}
        self.value["adapter"]["bind"] = "172.30.251.7"
        self.save()
        config = load_config(self.config_path)
        self.assertEqual(services.endpoints(config)["adapter"], "http://172.30.251.7:19880")
        self.assertEqual(config.gateway.adapter_url, "http://172.30.251.7:19880")

    def test_version_one_configuration_is_rejected(self):
        message = self.rejected(lambda data: data.__setitem__("version", 1))
        self.assertIn("version", message)

    def test_configuration_without_a_gateway_is_rejected(self):
        self.rejected(lambda data: data.pop("gateway"))

    def test_non_object_configuration_is_a_configuration_error(self):
        for value in ("null", "42", "[]", '"text"'):
            with self.subTest(value=value):
                self.config_path.write_text(value)
                with self.assertRaises(ConfigError):
                    load_config(self.config_path)
        self.save()

    def test_invalid_profiles_are_rejected_without_echo(self):
        def container(field, value):
            return lambda data: data["container"].__setitem__(field, value)

        cases = [
            container("core_address", "8.8.8.8"),
            container("core_address", "0.0.0.0"),
            container("core_address", "PRIVATE"),
            container("lan", {"parent": "eth0"}),
            container("core_subnet", "172.30.250.0/29"),
            container("studio_hosts", "PRIVATE"),
            container("studio_hosts", ["PRIVATE:1"]),
            container("studio_hosts", ["bad_PRIVATE.local"]),
            container("studio_hosts", [f"h{n}.local" for n in range(9)]),
            lambda data: data["adapter"].__setitem__("bind", "0.0.0.0"),
            lambda data: data.__setitem__("setup", {"bind": "127.0.0.1"}),
            lambda data: data["gateway"].__setitem__("lan_ip", "8.8.8.8"),
            lambda data: data["gateway"].__setitem__("timezone", "Nowhere/PRIVATE"),
        ]
        for number, change in enumerate(cases):
            with self.subTest(case=number):
                self.rejected(change)

    def test_files_may_be_anywhere_the_container_can_read(self):
        outside = self.base / "elsewhere"
        outside.mkdir()
        (outside / "targets.json").write_text("{}")
        self.value["adapter"]["targets_file"] = "../elsewhere/targets.json"
        self.save()
        self.assertEqual(load_config(self.config_path).targets_file,
                         (outside / "targets.json").resolve())

    # Endpoints -------------------------------------------------------------

    def test_endpoints_use_the_core_address(self):
        urls = services.endpoints(load_config(self.config_path))
        self.assertEqual(urls, {"adapter": "http://172.30.250.3:19880",
                                "setup": ("172.30.250.3", 19881),
                                "bridge": "http://172.30.250.3"})

    def test_config_without_targets_file_loads(self):
        del self.value["adapter"]["targets_file"]
        self.save()
        config = load_config(self.config_path)
        self.assertIsNone(config.targets_file)
        self.assertEqual(config.registry_file, self.state / "lights.json")

    def test_a_paired_bridge_is_used_only_without_explicit_hue_files(self):
        config = load_config(self.config_path)
        self.assertIsNone(config.hue_config_file)
        self.assertNotIn("--hue-config", config.arguments())
        paired = self.state / "hue-bridge.json"
        paired.write_text("{}")
        config = load_config(self.config_path)
        self.assertEqual((config.hue_config_file, config.hue_stream_config_file),
                         (paired, paired))
        self.assertTrue(config.hue_play)
        arguments = config.arguments()
        self.assertEqual(arguments[arguments.index("--hue-config") + 1], str(paired))
        self.assertEqual(arguments[arguments.index("--hue-stream-config") + 1], str(paired))
        self.assertIn("--hue-play", arguments)
        self.assertEqual(arguments[arguments.index("--lan-address") + 1], "192.168.99.10")
        # Explicit files always win over the paired bridge.
        self.value["adapter"].update(hue_config_file="../state/hue.json", hue_play=True)
        self.save()
        config = load_config(self.config_path)
        self.assertEqual(config.hue_config_file, self.state / "hue.json")
        self.assertIsNone(config.hue_stream_config_file)

    # Owner hold ------------------------------------------------------------

    def test_hold_waits_in_the_profile_instead_of_exiting(self):
        hold = self.state / "live-output-hold"
        hold.write_text("")
        started, sleeps = [], []

        def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 2:
                hold.unlink()

        runtime = type("Runtime", (), {"main": staticmethod(
            lambda *args, **kwargs: started.append(args))})
        with patch.object(cli, "prepare", lambda config, **kwargs: (runtime, None)):
            code = cli._adapter(load_config(self.config_path), False, sleep=sleep)
        self.assertEqual(code, 0)
        self.assertEqual(sleeps, [cli.HOLD_POLL_SECONDS] * 2)
        self.assertEqual(len(started), 1)


class StudioHostTests(unittest.TestCase):
    def test_listed_lan_names_are_accepted_and_others_refused(self):
        with TemporaryDirectory(prefix="tmp_studio_hosts_") as folder:
            server = ThreadingHTTPServer(("127.0.0.1", 0), studio.handler_for(
                Path(folder) / "missing-token", hosts=("diskstation.local",)))
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            port = server.server_port
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

            def status(**headers):
                request = urllib.request.Request(f"http://127.0.0.1:{port}/api/layout",
                                                 headers=headers)
                try:
                    with opener.open(request) as response:
                        return response.status
                except urllib.error.HTTPError as error:
                    error.close()
                    return error.code

            try:
                lan = f"diskstation.local:{port}"
                self.assertEqual(status(Host=lan), 200)
                self.assertEqual(status(Host=lan, Origin=f"http://{lan}"), 200)
                self.assertEqual(status(Host=f"other.local:{port}"), 403)
                self.assertEqual(status(Host=lan, Origin="http://evil.example"), 403)
                self.assertEqual(status(Host=lan, **{"Sec-Fetch-Site": "cross-site"}), 403)
            finally:
                server.shutdown()
                server.server_close()
                thread.join()


if __name__ == "__main__":
    unittest.main()
