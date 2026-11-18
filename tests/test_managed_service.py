"""Versioned release, supervisor rendering and packaged Setup Studio, offline only."""

import hashlib
import json
import plistlib
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

from smart_lighting_core import __version__, services
from smart_lighting_core.config import GATEWAY_IMAGE, ConfigError, load_config
from smart_lighting_core.dist import build_release

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
            "version": 1,
            "state_dir": "state",
            "adapter": {"targets_file": "targets.json", "token_file": "state/adapter-token",
                        "power_owner_token_file": "state/owner-token", "bind": "0.0.0.0",
                        "port": free_port()},
            "setup": {"port": free_port()},
            "gateway": {"lan_ip": "192.168.99.10", "mac": "02:00:00:00:00:0A",
                        "live_output": True, "hue_play": True, "entertainment": True},
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
        self.assertEqual((config.setup.bind, config.setup.port),
                         ("127.0.0.1", self.value["setup"]["port"]))
        self.assertEqual(config.gateway.image, GATEWAY_IMAGE)
        self.assertEqual(config.gateway.container_name, "smart-lighting-gateway")
        self.assertEqual(config.gateway.adapter_url,
                         f"http://host.docker.internal:{self.value['adapter']['port']}")
        self.assertEqual(config.gateway.environment()["PROBE_HUE_STRIP"], "false")
        self.assertEqual(config.gateway.environment()["PROBE_ENTERTAINMENT"], "true")
        self.assertEqual(config.logs_dir, config.state_dir / "logs")
        del self.value["gateway"], self.value["setup"]
        self.save()
        config = load_config(self.config_path)
        self.assertIsNone(config.gateway)
        self.assertEqual(config.setup.port, 19881)

    def test_invalid_gateway_values_are_rejected_without_echo(self):
        for field, value in (("image", "diyhue/core:latest"), ("lan_ip", "8.8.8.8"),
                             ("lan_ip", "0.0.0.0"), ("mac", "PRIVATE-MAC"),
                             ("adapter_url", "http://user:PRIVATE@host:1"),
                             ("container_name", "../PRIVATE"), ("live_output", "yes"),
                             ("unexpected", 1)):
            with self.subTest(field=field):
                saved = dict(self.value["gateway"])
                self.value["gateway"][field] = value
                self.save()
                with self.assertRaises(ConfigError) as caught:
                    load_config(self.config_path)
                self.assertNotIn("PRIVATE", str(caught.exception))
                self.value["gateway"] = saved
        self.value["setup"] = {"bind": "8.8.8.8"}
        self.save()
        with self.assertRaises(ConfigError):
            load_config(self.config_path)

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
                             (ROOT / "experiments/hue-bridge" / (name + ".py")).read_bytes())
        self.assertEqual(set(files), {services.ARCHIVE, *(
            f"gateway/{name}.py" for name in services.GATEWAY_MODULES)})

    def test_release_refuses_existing_output(self):
        existing = Path(self.temp.name) / "existing"
        existing.mkdir()
        (existing / "keep").write_text("peer")
        with self.assertRaises(FileExistsError):
            build_release(existing)
        self.assertEqual((existing / "keep").read_text(), "peer")

    def test_development_compose_mounts_the_same_gateway_modules(self):
        text = (ROOT / "experiments/hue-bridge/compose.yaml").read_text()
        mounted = re.findall(r"\./(\w+)\.py:/probe/\1\.py:ro", text)
        self.assertEqual(sorted(mounted), sorted(services.GATEWAY_MODULES))

    # Supervisors ----------------------------------------------------------

    def test_services_render_absolute_supervisors_without_credentials(self):
        output = self.folder / "services"
        result = self.run_archive("services", "--config", str(self.config_path),
                                  "--release", str(self.release), "--output", str(output))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["rendered"], [
            "com.smart-lighting.adapter.plist", "com.smart-lighting.setup.plist",
            "gateway-compose.json"])
        state = self.state.resolve()
        for label, command in ((services.ADAPTER_LABEL, "adapter"),
                               (services.SETUP_LABEL, "setup")):
            agent = plistlib.loads((output / f"{label}.plist").read_bytes())
            self.assertEqual(agent["Label"], label)
            self.assertEqual(agent["ProgramArguments"][1:], [
                "-I", "-B", str(self.release.resolve() / services.ARCHIVE), command,
                "--config", str(self.config_path.resolve())])
            self.assertTrue(Path(agent["ProgramArguments"][0]).is_absolute())
            self.assertEqual(agent["KeepAlive"], {"SuccessfulExit": False})
            self.assertTrue(agent["RunAtLoad"])
            self.assertEqual(agent["WorkingDirectory"], str(state))
            self.assertEqual(agent["StandardErrorPath"], str(state / "logs" / f"{command}.log"))
        compose = json.loads((output / "gateway-compose.json").read_text())
        gateway = compose["services"]["gateway"]
        self.assertEqual(gateway["restart"], "unless-stopped")
        self.assertEqual(gateway["image"], GATEWAY_IMAGE)
        self.assertEqual(gateway["container_name"], "smart-lighting-gateway")
        self.assertEqual(gateway["volumes"][0], f"{state}:/opt/hue-emulator/config")
        self.assertEqual(len(gateway["volumes"]), 1 + len(services.GATEWAY_MODULES))
        for volume in gateway["volumes"][1:]:
            self.assertTrue(volume.startswith(str(self.release.resolve() / "gateway") + "/"))
            self.assertTrue(volume.endswith(":ro"))
        self.assertEqual(gateway["environment"]["IP"], "192.168.99.10")
        self.assertEqual(gateway["environment"]["PROBE_LIVE_OUTPUT"], "true")
        text = "".join(path.read_text(errors="ignore") for path in output.iterdir())
        self.assertNotIn(TOKEN, text)
        self.assertNotIn("b" * 64, text)
        again = self.run_archive("services", "--config", str(self.config_path),
                                 "--release", str(self.release), "--output", str(output))
        self.assertNotEqual(again.returncode, 0)

    def test_services_reject_a_release_of_another_version(self):
        other = self.folder / "other-release"
        other.mkdir()
        (other / "manifest.json").write_text(json.dumps({"version": "0.0.0"}))
        result = self.run_archive("services", "--config", str(self.config_path),
                                  "--release", str(other), "--output", str(self.folder / "o"))
        self.assertEqual(result.returncode, 2)
        self.assertIn("version", result.stderr)
        self.assertFalse((self.folder / "o").exists())

    # Packaged Setup Studio ------------------------------------------------

    def test_packaged_setup_serves_archive_assets_and_stops_on_sigterm(self):
        port = self.value["setup"]["port"]
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
            source = ROOT / "experiments/setup-studio/static"
            self.assertEqual(index, (source / "index.html").read_bytes())
            with opener.open(url + "/app.js", timeout=1) as response:
                self.assertEqual(response.read(), (source / "app.js").read_bytes())
            with opener.open(url + "/api/pilot", timeout=2) as response:
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

    # Readiness ------------------------------------------------------------

    def test_ready_reports_unavailable_adapter_without_credentials(self):
        del self.value["gateway"]
        self.save()
        result = self.run_archive("ready", "--config", str(self.config_path), "--timeout", "0")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout), {
            "adapter": False, "adapter_version": None, "gateway": None, "ready": False})
        self.assertNotIn(TOKEN, result.stdout + result.stderr)

    def test_ready_accepts_an_authorized_adapter_status(self):
        del self.value["gateway"]
        self.value["adapter"]["bind"] = "127.0.0.1"
        self.save()
        seen = []

        class Adapter(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                seen.append(self.headers.get("X-Adapter-Token"))
                body = json.dumps({"service": {"version": "9.9.9"}}).encode()
                self.send_response(200 if self.path == "/status" else 404)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", self.value["adapter"]["port"]), Adapter)
        worker = threading.Thread(target=server.serve_forever)
        worker.start()
        try:
            result = self.run_archive("ready", "--config", str(self.config_path),
                                      "--timeout", "3")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), {
                "adapter": True, "adapter_version": "9.9.9", "gateway": None, "ready": True})
            self.assertEqual(seen[-1], TOKEN)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(2)

    def test_ready_waits_for_the_gateway_when_configured(self):
        config = load_config(self.config_path)
        calls = []

        def fake_get(url, headers=None, timeout=1.0):
            calls.append(url)
            return {"service": {"version": "x"}} if url.endswith("/status") else None

        original = services._get
        services._get = fake_get
        try:
            clock = iter([0, 0, 1, 2, 3])
            result = services.ready(config, TOKEN, timeout=2, clock=lambda: next(clock),
                                    sleep=lambda _: None)
        finally:
            services._get = original
        self.assertEqual(result, {"adapter": True, "adapter_version": "x", "gateway": False,
                                  "ready": False})
        self.assertIn("http://127.0.0.1/api/config", calls)


if __name__ == "__main__":
    unittest.main()
