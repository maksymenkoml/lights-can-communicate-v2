"""Versioned release, gateway configuration and packaged Setup Studio, offline only."""

import ast
import hashlib
import json
import re
import signal
import socket
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory

import nas_profile

from lights_can_communicate_v2_core import __version__, services
from lights_can_communicate_v2_core.config import ConfigError, load_config
from lights_can_communicate_v2_core.dist import build_release

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "a" * 48


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class ManagedServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = TemporaryDirectory(prefix="tmp_managed_service_")
        cls.release = Path(cls.temp.name) / "release"
        cls.manifest = build_release(cls.release)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        temp = TemporaryDirectory(prefix="tmp_managed_config_")
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.state = self.folder / "state"
        self.state.mkdir()
        (self.folder / "targets.json").write_text(json.dumps({"targets": [
            {"side": "left", "device": "AA:AA:AA:AA:AA:AA:AA:AA", "ip": "192.168.99.50"},
            {"side": "right", "device": "BB:BB:BB:BB:BB:BB:BB:BB", "ip": "192.168.99.51"},
        ]}))
        for name, content in (("adapter-token", TOKEN), ("owner-token", "b" * 64)):
            (self.state / name).write_text(content)
            (self.state / name).chmod(0o600)
        self.value = {
            "version": 2,
            "state_dir": "state",
            "adapter": {"targets_file": "targets.json", "token_file": "state/adapter-token",
                        "power_owner_token_file": "state/owner-token",
                        "port": free_port()},
            "setup": {"port": free_port()},
            **nas_profile.sections(live_output=True, hue_strip=True, hue_play=True,
                                   entertainment=True),
        }
        self.config_path = self.folder / "core.json"
        self.save()

    def save(self):
        self.config_path.write_text(json.dumps(self.value))

    def run_archive(self, *args, timeout=10):
        return subprocess.run(
            [sys.executable, "-I", "-B", str(self.release / services.ARCHIVE), *args],
            cwd=self.folder, capture_output=True, text=True, timeout=timeout)

    # Configuration -------------------------------------------------------

    def test_gateway_and_setup_sections_have_safe_defaults(self):
        config = load_config(self.config_path)
        self.assertEqual(config.setup.port, self.value["setup"]["port"])
        self.assertEqual(config.gateway.timezone, "Etc/UTC")
        self.assertEqual(config.gateway.adapter_url,
                         f"http://172.30.250.3:{self.value['adapter']['port']}")
        self.assertEqual(config.gateway.environment()["CONFIG_PATH"], "/opt/hue-emulator/config")
        self.assertEqual(config.gateway.environment("/data/state")["CONFIG_PATH"], "/data/state")
        self.assertEqual(config.gateway.environment()["TZ"], "Etc/UTC")
        self.assertEqual(config.gateway.environment()["PROBE_HUE_STRIP"], "true")
        self.assertEqual(config.gateway.environment()["PROBE_GOVEE_GRADIENT"], "false")
        self.assertEqual(config.gateway.environment()["PROBE_ENTERTAINMENT"], "true")
        del self.value["setup"]
        self.save()
        self.assertEqual(load_config(self.config_path).setup.port, 19881)

    def test_arguments_pass_registry(self):
        config = load_config(self.config_path)
        arguments = config.arguments()
        self.assertEqual(config.registry_file, config.state_dir / "lights.json")
        self.assertEqual(arguments[arguments.index("--registry") + 1], str(config.registry_file))
        self.assertNotIn("--targets", arguments)
        self.assertNotIn("--native-profile", arguments)

    def test_power_authority_defaults_to_the_remote_owner(self):
        config = load_config(self.config_path)
        self.assertEqual(config.power_authority, "owner")
        self.assertEqual(config.arguments()[-2:], ["--power-authority", "owner"])
        self.value["adapter"]["power_authority"] = "homekit"
        self.save()
        self.assertEqual(load_config(self.config_path).arguments()[-1], "homekit")
        self.value["adapter"]["power_authority"] = "PRIVATE"
        self.save()
        with self.assertRaises(ConfigError) as caught:
            load_config(self.config_path)
        self.assertNotIn("PRIVATE", str(caught.exception))

    def test_invalid_gateway_values_are_rejected_without_echo(self):
        for field, value in (("image", "diyhue/core:latest"), ("lan_ip", "8.8.8.8"),
                             ("timezone", "Mars/PRIVATE"), ("timezone", "../PRIVATE"),
                             ("lan_ip", "0.0.0.0"), ("mac", "PRIVATE-MAC"),
                             ("adapter_url", "http://user:PRIVATE@host:1"),
                             ("container_name", "PRIVATE"), ("live_output", "yes"),
                             ("unexpected", 1)):
            with self.subTest(field=field):
                saved = dict(self.value["gateway"])
                self.value["gateway"][field] = value
                self.save()
                with self.assertRaises(ConfigError) as caught:
                    load_config(self.config_path)
                self.assertNotIn("PRIVATE", str(caught.exception))
                self.value["gateway"] = saved
        self.value["adapter"]["bind"] = "127.0.0.1"
        self.save()
        with self.assertRaises(ConfigError) as caught:
            load_config(self.config_path)
        self.assertIn("core address", str(caught.exception))
        del self.value["adapter"]["bind"]
        self.value["setup"] = {"bind": "8.8.8.8"}
        self.save()
        with self.assertRaises(ConfigError):
            load_config(self.config_path)

    def test_entertainment_requires_the_strip_and_play_lights(self):
        # The gateway refuses to start Desk Entertainment without all six
        # virtual lights; Docker would restart it in a loop.
        for missing in ("hue_strip", "hue_play"):
            with self.subTest(missing=missing):
                saved = dict(self.value["gateway"])
                self.value["gateway"][missing] = False
                self.save()
                with self.assertRaisesRegex(ConfigError, "entertainment requires"):
                    load_config(self.config_path)
                self.value["gateway"] = saved
        self.value["gateway"].update(entertainment=False, hue_strip=False, hue_play=False)
        self.save()
        self.assertFalse(load_config(self.config_path).gateway.entertainment)

    # Release --------------------------------------------------------------

    def test_release_manifest_matches_files_and_sources(self):
        self.assertEqual(self.manifest["version"], __version__)
        files = self.manifest["files"]
        self.assertIn(services.ARCHIVE, files)
        for name, digest in files.items():
            self.assertEqual(hashlib.sha256((self.release / name).read_bytes()).hexdigest(),
                             digest)
        for name in services.GATEWAY_MODULES:
            self.assertEqual((self.release / "gateway" / (name + ".py")).read_bytes(),
                             (ROOT / "bridge" / (name + ".py")).read_bytes())
        plugin = self.manifest["plugin"]["file"]
        self.assertEqual(set(files), {services.ARCHIVE, plugin, *(
            f"gateway/{name}.py" for name in services.GATEWAY_MODULES)})

    def test_release_refuses_existing_output(self):
        existing = Path(self.temp.name) / "existing"
        existing.mkdir()
        (existing / "keep").write_text("peer")
        with self.assertRaises(FileExistsError):
            build_release(existing)
        self.assertEqual((existing / "keep").read_text(), "peer")

    def test_the_gateway_ships_every_bridge_module_its_bootstrap_imports(self):
        # A module missing here passes every test from the source tree, but the
        # released gateway fails to start (seen with scene_edits).
        def local_imports(name):
            tree = ast.parse((ROOT / "bridge" / (name + ".py")).read_text())
            found = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    found |= {alias.name.split(".")[0] for alias in node.names}
                elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                    found.add(node.module.split(".")[0])
            return {item for item in found if (ROOT / "bridge" / (item + ".py")).exists()}

        needed, pending = set(), ["bootstrap"]
        while pending:
            for item in local_imports(pending.pop()) - needed:
                needed.add(item)
                pending.append(item)
        self.assertEqual(sorted(needed - set(services.GATEWAY_MODULES)), [])

    def test_development_compose_mounts_the_same_gateway_modules(self):
        text = (ROOT / "bridge/compose.yaml").read_text()
        mounted = re.findall(r"\./(\w+)\.py:/probe/\1\.py:ro", text)
        self.assertEqual(sorted(mounted), sorted(services.GATEWAY_MODULES))
        # The gateway reads the light registry with the core package.
        self.assertIn("../lights_can_communicate_v2_core:/probe/lights_can_communicate_v2_core:ro",
                      text)

    # Packaged Setup Studio ------------------------------------------------

    def test_packaged_setup_serves_archive_assets_and_stops_on_sigterm(self):
        port = self.value["setup"]["port"]
        # Light Studio listens on the core address only; here that is the loopback.
        self.value["container"]["core_address"] = "127.0.0.1"
        self.value["adapter"].pop("bind", None)
        self.config_path.write_text(json.dumps(self.value))
        process = subprocess.Popen(
            [sys.executable, "-I", "-B", str(self.release / services.ARCHIVE), "setup",
             "--config", str(self.config_path)],
            cwd=self.folder, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            url = f"http://127.0.0.1:{port}"
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            deadline = time.monotonic() + 20
            while True:
                try:
                    with opener.open(url + "/", timeout=1) as response:
                        index = response.read()
                    break
                except OSError:
                    if process.poll() is not None or time.monotonic() > deadline:
                        process.kill()
                        self.fail("Packaged setup did not start: " + process.communicate()[1])
                    time.sleep(0.1)
            source = ROOT / "studio/static"
            self.assertEqual(index, (source / "index.html").read_bytes())
            for path, name, kind in (
                    ("/app.js", "app.js", "text/javascript; charset=utf-8"),
                    ("/style.css", "style.css", "text/css; charset=utf-8"),
                    ("/fonts/commissioner-latin.woff2", "fonts/commissioner-latin.woff2",
                     "font/woff2")):
                with opener.open(url + path, timeout=1) as response:
                    self.assertEqual(response.headers["Content-Type"], kind)
                    self.assertEqual(response.read(), (source / name).read_bytes())
            for path in ("/fonts/OFL.txt", "/fonts/", "/static/app.js", "/server.py"):
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    opener.open(url + path, timeout=1)
                caught.exception.close()
                self.assertEqual(caught.exception.code, 404)
            with opener.open(url + "/api/live", timeout=2) as response:
                self.assertFalse(json.load(response)["connected"])
            process.send_signal(signal.SIGTERM)
            self.assertEqual(process.wait(timeout=5), 0)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stdout.close()
            process.stderr.close()

    def test_setup_refuses_a_public_credential_file(self):
        (self.state / "adapter-token").chmod(0o644)
        result = self.run_archive("setup", "--config", str(self.config_path))
        self.assertEqual(result.returncode, 2)
        self.assertNotIn(TOKEN, result.stderr)


if __name__ == "__main__":
    unittest.main()
