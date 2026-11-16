"""Run the actual distribution outside the checkout with synthetic state only."""

import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
import unittest
import urllib.request
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
PINNED_LINUX = (
    sys.platform == "linux"
    and Path("/opt/hue-emulator").is_dir()
    and os.environ.get("SMART_LIGHTING_ISOLATED_TESTS") == "1"
)


class CoreDistributionTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix="tmp_core_distribution_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.artifact = self.root / "adapter.pyz"
        build = subprocess.run(
            [
                sys.executable,
                "-B",
                "-m",
                "smart_lighting_core.build",
                "--output",
                str(self.artifact),
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )
        self.assertEqual(build.returncode, 0, build.stderr)
        self.folder = self.root / "settings"
        self.folder.mkdir()
        self.state = self.folder / "state"
        self.state.mkdir()
        self.targets = self.folder / "targets.json"
        self.targets.write_text(
            json.dumps(
                {
                    "targets": [
                        {
                            "side": "left",
                            "device": "AA:AA:AA:AA:AA:AA:AA:AA",
                            "ip": "192.168.99.50",
                            "brightness_scale": 0.65,
                            "max_gradient_packets_per_second": 4,
                        },
                        {
                            "side": "right",
                            "device": "BB:BB:BB:BB:BB:BB:BB:BB",
                            "ip": "192.168.99.51",
                            "brightness_scale": 0.65,
                            "max_gradient_packets_per_second": 2,
                        },
                    ]
                }
            )
        )
        for name, content in (("adapter-token", "a" * 48), ("owner-token", "b" * 64)):
            path = self.state / name
            path.write_text(content)
            path.chmod(0o600)
        self.config = self.folder / "core.json"
        self.value = {
            "version": 1,
            "state_dir": "state",
            "adapter": {
                "targets_file": "targets.json",
                "token_file": "state/adapter-token",
                "power_owner_token_file": "state/owner-token",
                "bind": "127.0.0.1",
                "port": 19880,
                "gradient": True,
            },
        }
        self.save()
        self.cwd = self.root / "elsewhere"
        self.cwd.mkdir()

    def save(self):
        self.config.write_text(json.dumps(self.value))

    def run_archive(self, *args):
        return subprocess.run(
            [sys.executable, "-I", "-B", str(self.artifact), *args],
            cwd=self.cwd,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_archive_is_reproducible_allowlisted_and_self_contained(self):
        second = self.root / "second.pyz"
        result = subprocess.run(
            [sys.executable, "-B", "-m", "smart_lighting_core.build", "--output", str(second)],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.artifact.read_bytes(), second.read_bytes())
        with zipfile.ZipFile(self.artifact) as bundle:
            names = bundle.namelist()
            self.assertIn("live_adapter.py", names)
            self.assertIn("smart_lighting.py", names)
            self.assertIn("smart_lighting_core/cli.py", names)
            self.assertFalse(any("state/" in name or "tests/" in name for name in names))
            self.assertFalse(any(".lighting" in name or name.endswith(".jsonl") for name in names))
            manifest = json.loads(bundle.read("core-manifest.json"))
            for name, digest in manifest["files"].items():
                self.assertEqual(hashlib.sha256(bundle.read(name)).hexdigest(), digest)
        result = self.run_archive("--version")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("0.1.0.dev1", result.stdout)
        # Changing CWD or placing a module beside the artifact must not hijack imports.
        (self.cwd / "live_adapter.py").write_text("raise RuntimeError('CWD import hijacked')")
        (self.root / "desk_layout.py").write_text("raise RuntimeError('parent import hijacked')")
        result = self.run_archive("--config", str(self.config), "--check")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertTrue(data["gradient"])
        self.assertEqual(data["targets"], 2)
        self.assertNotIn("a" * 48, result.stdout + result.stderr)
        self.assertNotIn("b" * 64, result.stdout + result.stderr)

    def test_builder_refuses_existing_output_without_changing_it(self):
        before = self.artifact.read_bytes()
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                "-m",
                "smart_lighting_core.build",
                "--output",
                str(self.artifact),
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.artifact.read_bytes(), before)

    def test_check_is_read_only_and_never_initializes_network(self):
        before = {p: p.read_bytes() for p in self.folder.rglob("*") if p.is_file()}
        code = """
import socket,ssl,sys
sys.path.insert(0, sys.argv[1])
def forbidden(*args, **kwargs):
    raise AssertionError("Check attempted network setup")
socket.socket = forbidden
from smart_lighting_core.cli import main
raise SystemExit(main(["--config", sys.argv[2], "--check"]))
"""
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-c", code, str(self.artifact), str(self.config)],
            cwd=self.cwd,
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(before, {p: p.read_bytes() for p in self.folder.rglob("*") if p.is_file()})

    def test_strict_config_rejects_bad_values_without_echoing_secrets(self):
        cases = [
            {"version": True},
            {"version": 2},
            {"adapter": {**self.value["adapter"], "bind": True}},
            {"unexpected": "a" * 48},
            {"state_dir": ""},
            {"adapter": {**self.value["adapter"], "port": True}},
            {"adapter": {**self.value["adapter"], "port": 65536}},
            {"adapter": {**self.value["adapter"], "gradient": "true"}},
            {"adapter": {**self.value["adapter"], "bind": "0.0.0.0; echo secret"}},
            {"adapter": {**self.value["adapter"], "unknown": "b" * 64}},
            {"adapter": {**self.value["adapter"], "hue_play": True}},
            {"adapter": {**self.value["adapter"], "hue_stream_config_file": "stream.json"}},
            {"adapter": {**self.value["adapter"], "targets_file": "missing.json"}},
        ]
        for change in cases:
            with self.subTest(change=list(change)):
                self.config.write_text(json.dumps({**self.value, **change}))
                result = self.run_archive("--config", str(self.config), "--check")
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("a" * 48, result.stdout + result.stderr)
                self.assertNotIn("b" * 64, result.stdout + result.stderr)
                self.assertNotIn("Traceback", result.stderr)
        self.config.write_text('{"version":1,"version":1,"adapter":{}}')
        self.assertNotEqual(self.run_archive("--config", str(self.config), "--check").returncode, 0)

    def test_explicit_state_hold_blocks_start_even_when_targets_are_elsewhere(self):
        (self.state / "live-output-hold").write_text("Owner hold")
        result = self.run_archive("--config", str(self.config))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("hold", result.stderr.lower())
        check = self.run_archive("--config", str(self.config), "--check")
        self.assertEqual(check.returncode, 0, check.stderr)
        self.assertTrue(json.loads(check.stdout)["hold"])

    def test_credentials_are_bounded_private_and_not_repeated_in_errors(self):
        owner = self.state / "owner-token"
        for content in ["a" * 48, "b" * 128 + "  " + "SECRET_TRAILER", "BAD_SECRET"]:
            with self.subTest(case=len(content)):
                owner.write_text(content)
                result = self.run_archive("--config", str(self.config), "--check")
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn(content, result.stdout + result.stderr)
                self.assertNotIn("SECRET_TRAILER", result.stdout + result.stderr)
        owner.write_text("b" * 64)
        owner.chmod(0o644)
        result = self.run_archive("--config", str(self.config), "--check")
        if os.name == "posix":
            self.assertNotEqual(result.returncode, 0)

    def test_native_video_preflight_checks_files_without_device_io(self):
        hue = {
            "bridge": "192.168.99.2",
            "hue_key": "c" * 64,
            "light": "98250ec6-d7fd-4b47-ace6-ff449ea3066f",
        }
        stream = {
            "bridge": "192.168.99.2",
            "hue_key": "d" * 64,
            "client_key": "e" * 32,
            "area_id": "10c81079-56a6-45c4-8149-55185f703083",
        }
        (self.folder / "hue.json").write_text(json.dumps(hue))
        (self.folder / "stream.json").write_text(json.dumps(stream))
        self.value["adapter"].update(
            {
                "hue_config_file": "hue.json",
                "hue_play": True,
                "hue_stream_config_file": "stream.json",
            }
        )
        self.save()
        result = self.run_archive("--config", str(self.config), "--check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["native_video"])
        stream["bridge"] = "192.168.99.3"
        (self.folder / "stream.json").write_text(json.dumps(stream))
        self.assertNotEqual(self.run_archive("--config", str(self.config), "--check").returncode, 0)

    @unittest.skipUnless(PINNED_LINUX, "Real process fixture requires isolated pinned Linux image")
    def test_packaged_http_and_sigterm_cleanup_with_synthetic_targets(self):
        # This test is only run in --network none, without host ports or private mounts.
        self.value["adapter"]["port"] = 19883
        self.save()
        process = subprocess.Popen(
            [sys.executable, "-I", "-B", str(self.artifact), "--config", str(self.config)],
            cwd=self.cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(lambda: process.poll() is None and process.kill())
        request = urllib.request.Request(
            "http://127.0.0.1:19883/status", headers={"X-Adapter-Token": "a" * 48}
        )
        end = time.monotonic() + 5
        status = None
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        while time.monotonic() < end and process.poll() is None:
            try:
                with opener.open(request, timeout=0.2) as response:
                    status = json.load(response)
                break
            except OSError:
                time.sleep(0.05)
        if status is None:
            process.kill()
            stdout, stderr = process.communicate(timeout=3)
            self.fail("Packaged process did not become ready: " + stdout + stderr)
        self.assertEqual(status["service"]["version"], "0.1.0.dev1")
        self.assertEqual(status["commands_sent"], 0)
        self.assertEqual(set(status["power_owner"]["roles"]), {"left", "right"})
        os.kill(process.pid, signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0, stdout + stderr)
        with socket.socket() as client:
            self.assertNotEqual(client.connect_ex(("127.0.0.1", 19883)), 0)


if __name__ == "__main__":
    unittest.main()
