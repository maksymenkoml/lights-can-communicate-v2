"""NAS container profile: configuration, compose project, endpoints and hold, offline only."""

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

from lights_can_communicate_v2_core import cli, install, services
from lights_can_communicate_v2_core.config import GATEWAY_IMAGE, ConfigError, load_config
from lights_can_communicate_v2_core.dist import build_release

spec = importlib.util.spec_from_file_location(
    "setup_studio", Path(__file__).resolve().parents[1] / "studio/server.py"
)
studio = importlib.util.module_from_spec(spec)
spec.loader.exec_module(studio)

TOKEN = "a" * 48


class ContainerProfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = TemporaryDirectory(prefix="tmp_container_release_")
        cls.release = Path(cls.temp.name) / "release"
        build_release(cls.release)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

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
            "version": 1,
            "state_dir": "../state",
            "adapter": {"targets_file": "../state/targets.json",
                        "token_file": "../state/adapter-token",
                        "power_owner_token_file": "../state/owner-token", "port": 19880},
            "setup": {"port": 19881},
            "gateway": {"lan_ip": "192.168.99.10", "mac": "02:00:00:00:00:0A",
                        "live_output": True, "hue_strip": True, "hue_play": True,
                        "entertainment": True},
            "container": {
                "lan": {"parent": "ovs_eth0", "subnet": "192.168.99.0/24",
                        "router": "192.168.99.1", "adapter_ip": "192.168.99.11"},
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

    def test_profile_derives_core_addresses_and_defaults(self):
        config = load_config(self.config_path)
        container = config.container
        self.assertEqual((container.gateway_core, container.adapter_core, container.studio_core),
                         ("172.30.250.2", "172.30.250.3", "172.30.250.4"))
        self.assertEqual(config.bind, "172.30.250.3")
        self.assertEqual(config.setup.bind, "0.0.0.0")
        self.assertEqual(config.gateway.adapter_url, "http://172.30.250.3:19880")
        self.assertEqual(container.studio_hosts, ("192.168.99.5", "diskstation.local"))
        self.assertEqual(container.parent, "ovs_eth0")

    def test_configuration_without_the_profile_is_rejected(self):
        # The NAS container profile is the only supported runtime.
        for removed in (("container",), ("gateway",), ("container", "gateway")):
            with self.subTest(removed=removed):
                def change(data, removed=removed):
                    for section in removed:
                        del data[section]
                    data["adapter"]["bind"] = "127.0.0.1"
                message = self.rejected(change)
                self.assertIn("gateway and container sections", message)

    def test_non_object_configuration_is_a_configuration_error(self):
        for value in ("null", "42", "[]", '"text"'):
            with self.subTest(value=value):
                self.config_path.write_text(value)
                with self.assertRaises(ConfigError):
                    load_config(self.config_path)
        self.save()

    def test_invalid_profiles_are_rejected_without_echo(self):
        def lan(field, value):
            return lambda data: data["container"]["lan"].__setitem__(field, value)

        def container(field, value):
            return lambda data: data["container"].__setitem__(field, value)

        cases = [
            lan("parent", "eth0;PRIVATE"),
            lan("subnet", "8.8.8.0/24"),
            lan("router", "192.168.98.1"),
            lan("adapter_ip", "192.168.99.255"),
            lan("adapter_ip", "192.168.99.1"),
            lan("unexpected", "PRIVATE"),
            container("core_subnet", "172.30.250.0/30"),
            container("core_subnet", "192.168.99.0/28"),
            container("studio_hosts", []),
            container("studio_hosts", ["PRIVATE:1"]),
            container("studio_hosts", ["bad_PRIVATE.local"]),
            lambda data: data["gateway"].__setitem__("lan_ip", "192.168.99.11"),
            lambda data: data["gateway"].__setitem__("lan_ip", "192.168.98.10"),
            lambda data: data["adapter"].__setitem__("bind", "0.0.0.0"),
            lambda data: data.__setitem__("setup", {"bind": "127.0.0.1"}),
            lambda data: data.pop("gateway"),
            lambda data: data.pop("container"),
        ]
        for number, change in enumerate(cases):
            with self.subTest(case=number):
                self.rejected(change)

    def test_profile_refuses_files_outside_the_mounted_directories(self):
        outside = self.base / "elsewhere"
        outside.mkdir()
        (outside / "targets.json").write_text("{}")
        message = self.rejected(
            lambda data: data["adapter"].__setitem__("targets_file", "../elsewhere/targets.json"))
        self.assertIn("configuration directory or state_dir", message)

    # Compose project -----------------------------------------------------

    def render(self):
        output = self.base / "services"
        names = services.render(load_config(self.config_path), self.config_path, self.release,
                                output, link=self.base / "current")
        return names, json.loads((output / services.COMPOSE).read_text()), output

    def test_render_writes_one_project(self):
        names, project, output = self.render()
        self.assertEqual(names, [services.COMPOSE])
        self.assertNotIn(TOKEN, (output / services.COMPOSE).read_text())
        self.assertEqual(project["name"], "lights-can-communicate-v2")
        self.assertEqual(set(project["services"]), {"gateway", "adapter", "studio"})
        for service in project["services"].values():
            self.assertEqual(service["image"], GATEWAY_IMAGE)
            self.assertEqual(service["restart"], "unless-stopped")

    def test_render_places_components_on_the_lan_and_core_networks(self):
        _, project, _ = self.render()
        lan, core = project["networks"]["lan"], project["networks"]["core"]
        self.assertEqual(lan["driver"], "macvlan")
        self.assertEqual(lan["driver_opts"], {"parent": "ovs_eth0"})
        self.assertEqual(lan["ipam"]["config"],
                         [{"subnet": "192.168.99.0/24", "gateway": "192.168.99.1"}])
        self.assertEqual(core["ipam"]["config"],
                         [{"subnet": "172.30.250.0/29", "gateway": "172.30.250.1"}])
        gateway, adapter, studio_service = (project["services"][name]
                                            for name in ("gateway", "adapter", "studio"))
        self.assertEqual(gateway["networks"], {"lan": {"ipv4_address": "192.168.99.10"},
                                               "core": {"ipv4_address": "172.30.250.2"}})
        self.assertEqual(adapter["networks"], {"lan": {"ipv4_address": "192.168.99.11"},
                                               "core": {"ipv4_address": "172.30.250.3"}})
        self.assertEqual(studio_service["networks"], {"core": {"ipv4_address": "172.30.250.4"}})
        # Macvlan needs no published ports, and the host gateway is unreachable from it.
        self.assertNotIn("ports", gateway)
        self.assertNotIn("extra_hosts", gateway)
        self.assertNotIn("ports", adapter)
        self.assertEqual(studio_service["ports"], ["19881:19881/tcp"])
        self.assertEqual(gateway["environment"]["PROBE_ADAPTER_URL"], "http://172.30.250.3:19880")
        self.assertEqual(gateway["environment"]["IP"], "192.168.99.10")
        self.assertEqual(gateway["environment"]["MAC"], "02:00:00:00:00:0a")

    def test_render_runs_the_archive_through_the_installation_link(self):
        _, project, _ = self.render()
        current = self.base / "current"
        for name, command in (("adapter", "adapter"), ("studio", "setup")):
            service = project["services"][name]
            self.assertEqual(service["entrypoint"],
                             ["python3", "-I", "-B", f"{current}/{services.ARCHIVE}", command,
                              "--config", str(self.config_path)])
            self.assertEqual(service["volumes"], [
                f"{self.base / 'config'}:{self.base / 'config'}:ro",
                f"{self.state}:{self.state}",
                f"{current}:{current}:ro",
            ])
            self.assertEqual(service["working_dir"], str(self.state))
        self.assertIn(f"{current}/gateway/bootstrap.py:/probe/bootstrap.py:ro",
                      project["services"]["gateway"]["volumes"])

    # Endpoints -------------------------------------------------------------

    def test_endpoints_use_the_core_network(self):
        urls = services.endpoints(load_config(self.config_path))
        self.assertEqual(urls, {"adapter": "http://172.30.250.3:19880",
                                "setup": ("172.30.250.4", 19881),
                                "bridge": "http://172.30.250.2"})

    def test_installer_stop_check_probes_the_core_addresses(self):
        probed, fetched = [], []
        with (patch.object(install, "_answers", lambda *a: probed.append(a) or True),
              patch.object(services, "_get", lambda url, *a, **k: fetched.append(url) or {})):
            answering = install.running(load_config(self.config_path))
        self.assertEqual(answering, ["adapter", "setup", "gateway"])
        self.assertEqual(probed, [("172.30.250.3", 19880), ("172.30.250.4", 19881)])
        self.assertEqual(fetched, ["http://172.30.250.2/api/config"])

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
