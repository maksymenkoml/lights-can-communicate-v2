"""Offline contracts for staging, activation, rollback and interrupted transactions."""

import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from smart_lighting_core import __version__, services
from smart_lighting_core.dist import STATE_SCHEMAS, build_release
from smart_lighting_core.install import Installation, InstallError, verify

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "experiments/hue-bridge"))
from desk_layout import validate_layout  # noqa: E402

TOKEN = "a" * 48


def stopped(_config):
    return []


class InstallTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = TemporaryDirectory(prefix="tmp_install_release_")
        cls.release = Path(cls.temp.name) / "release"
        cls.manifest = build_release(cls.release)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        temp = TemporaryDirectory(prefix="tmp_install_base_")
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name) / "base"
        self.state = self.base / "state"
        (self.base / "config").mkdir(parents=True)
        self.state.mkdir()
        (self.base / "targets.json").write_text(json.dumps({"targets": [
            {"side": "left", "device": "AA:AA:AA:AA:AA:AA:AA:AA", "ip": "192.168.99.50"},
            {"side": "right", "device": "BB:BB:BB:BB:BB:BB:BB:BB", "ip": "192.168.99.51"},
        ]}))
        for name, content in (("adapter-token", TOKEN), ("owner-token", "b" * 64)):
            (self.state / name).write_text(content)
            (self.state / name).chmod(0o600)
        (self.state / "config.yaml").write_text("pairing: synthetic\n")
        (self.state / "desk-layout.json").write_text('{"version": 3, "marker": "before"}\n')
        (self.state / "logs").mkdir()
        (self.state / "logs" / "adapter.log").write_text("log\n")
        (self.state / "requests.jsonl").write_text("{}\n")
        (self.state / "adapter-stream-health.jsonl").write_text("{}\n")
        self.config = {
            "version": 1,
            "state_dir": str(self.state),
            "adapter": {"targets_file": str(self.base / "targets.json"),
                        "token_file": str(self.state / "adapter-token"),
                        "power_owner_token_file": str(self.state / "owner-token"),
                        "bind": "0.0.0.0", "port": 19990},
            "gateway": {"lan_ip": "192.168.99.10", "mac": "02:00:00:00:00:0A"},
        }
        (self.base / "config" / "core.json").write_text(json.dumps(self.config))
        self.install = Installation(self.base, probe=stopped)

    def place(self, version, state=None):
        """Install a copy of the test release under another version without staging."""
        target = self.base / "releases" / version
        shutil.copytree(self.release, target)
        manifest = json.loads((target / "manifest.json").read_text())
        manifest["version"] = version
        if state is not None:
            manifest["compatibility"]["state"] = state
        (target / "manifest.json").chmod(0o644)
        (target / "manifest.json").write_text(json.dumps(manifest))
        return target

    def layout(self):
        return json.loads((self.state / "desk-layout.json").read_text())["marker"]

    def journal(self):
        return json.loads((self.base / "install.json").read_text())

    # Release contract ----------------------------------------------------

    def test_manifest_declares_compatibility_and_a_reproducible_plugin(self):
        compatibility = self.manifest["compatibility"]
        self.assertEqual(compatibility["config_version"], [1])
        self.assertEqual(compatibility["core_api"], {"homekit": 1})
        self.assertEqual(compatibility["state"], STATE_SCHEMAS)
        plugin = compatibility["plugin"]
        self.assertIn(plugin["file"], self.manifest["files"])
        with TemporaryDirectory(prefix="tmp_install_again_") as again:
            second = build_release(Path(again) / "release")
        self.assertEqual(second["files"], self.manifest["files"])
        # The declared layout schema is the one the gateway validates.
        with self.assertRaises(ValueError):
            validate_layout({"version": STATE_SCHEMAS["desk_layout"] + 1})
        js = (Path(__file__).resolve().parents[1] / "homebridge/lib/client.js").read_text()
        self.assertIn(f"export const CORE_API = {compatibility['core_api']['homekit']};", js)

    # Staging -------------------------------------------------------------

    def test_stage_verifies_validates_and_never_activates(self):
        result = self.install.stage(self.release)
        self.assertEqual(result, {"version": __version__, "staged": True})
        staged = self.base / "releases" / __version__
        verify(staged)
        self.assertFalse((self.base / "current").exists())
        compose = json.loads((staged / "services" / "gateway-compose.json").read_text())
        mounts = compose["services"]["gateway"]["volumes"]
        self.assertTrue(all(str(self.base / "current") in m for m in mounts[1:]))
        self.assertEqual(self.install.stage(self.release)["staged"], False)

    def test_stage_refuses_tampered_or_conflicting_builds_and_bad_configuration(self):
        with TemporaryDirectory(prefix="tmp_install_bad_") as folder:
            copy = Path(folder) / "release"
            shutil.copytree(self.release, copy)
            module = copy / "gateway" / "bootstrap.py"
            module.chmod(0o644)
            module.write_text("tampered\n")
            with self.assertRaisesRegex(InstallError, "manifest"):
                self.install.stage(copy)
            (copy / "extra.py").write_text("")
            with self.assertRaisesRegex(InstallError, "differ"):
                self.install.stage(copy)
        self.config["version"] = 2
        (self.base / "config" / "core.json").write_text(json.dumps(self.config))
        with self.assertRaisesRegex(InstallError, "configuration version"):
            self.install.stage(self.release)
        self.config["version"] = 1
        self.config["adapter"]["targets_file"] = str(self.base / "missing.json")
        (self.base / "config" / "core.json").write_text(json.dumps(self.config))
        with self.assertRaisesRegex(InstallError, "does not validate"):
            self.install.stage(self.release)
        self.assertFalse((self.base / "releases" / __version__).exists())
        self.assertFalse((self.base / "releases" / (__version__ + ".partial")).exists())

    def test_a_different_build_of_an_installed_version_is_refused(self):
        self.place(__version__)
        manifest = json.loads((self.base / "releases" / __version__ / "manifest.json").read_text())
        manifest["files"]["gateway/bootstrap.py"] = "0" * 64
        (self.base / "releases" / __version__ / "manifest.json").write_text(json.dumps(manifest))
        with self.assertRaisesRegex(InstallError, "different build"):
            self.install.stage(self.release)

    # Activation and rollback --------------------------------------------

    def test_activation_requires_stopped_components_and_snapshots_durable_state(self):
        self.place("1.0.0")
        busy = Installation(self.base, probe=lambda _: ["adapter", "gateway"])
        with self.assertRaisesRegex(InstallError, "adapter, gateway"):
            busy.activate("1.0.0")
        self.assertFalse((self.base / "install.json").exists())
        result = self.install.activate("1.0.0")
        self.assertEqual(os.readlink(self.base / "current"), "releases/1.0.0")
        backup = self.base / "backups" / result["backup"]
        self.assertEqual(sorted(p.name for p in backup.iterdir()),
                         ["adapter-token", "config.yaml", "desk-layout.json", "owner-token"])
        self.assertEqual(backup.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.journal()["current_backup"], result["backup"])
        self.assertFalse(self.install.activate("1.0.0")["changed"])

    def test_rollback_without_a_schema_change_keeps_newer_state(self):
        self.place("1.0.0")
        self.place("1.1.0")
        self.install.activate("1.0.0")
        self.install.activate("1.1.0")
        (self.state / "desk-layout.json").write_text('{"version": 3, "marker": "edited"}\n')
        result = self.install.rollback()
        self.assertEqual(os.readlink(self.base / "current"), "releases/1.0.0")
        self.assertIsNone(result["restored"])
        self.assertEqual(self.layout(), "edited")
        self.assertEqual(self.journal()["previous"], None)
        with self.assertRaisesRegex(InstallError, "No previous"):
            self.install.rollback()

    def test_rollback_across_a_schema_change_restores_the_upgrade_snapshot(self):
        self.place("1.0.0")
        self.place("2.0.0", state={**STATE_SCHEMAS, "desk_layout": 4})
        self.install.activate("1.0.0")
        upgrade = self.install.activate("2.0.0")
        (self.state / "desk-layout.json").write_text('{"version": 4, "marker": "migrated"}\n')
        (self.state / "new-state.json").write_text("{}\n")
        result = self.install.rollback()
        self.assertEqual(result["restored"], upgrade["backup"])
        self.assertEqual(self.layout(), "before")
        self.assertFalse((self.state / "new-state.json").exists())
        # Nothing is lost: the migrated state is in the rollback snapshot.
        saved = self.base / "backups" / result["backup"] / "desk-layout.json"
        self.assertIn("migrated", saved.read_text())
        # Live logs are neither snapshotted nor removed.
        self.assertTrue((self.state / "logs" / "adapter.log").exists())
        self.assertTrue((self.state / "requests.jsonl").exists())

    def test_every_interrupted_step_is_completed_by_the_next_command(self):
        self.place("1.0.0")
        self.place("2.0.0", state={**STATE_SCHEMAS, "desk_layout": 4})
        self.install.activate("1.0.0")
        steps = (("_point", "activate"), ("snapshot", "activate"),
                 ("restore", "rollback"), ("_point", "rollback"))
        for method, operation in steps:
            with self.subTest(method=method, operation=operation):
                if operation == "rollback" and self.journal()["current"] != "2.0.0":
                    self.install.activate("2.0.0")
                if operation == "activate" and self.journal()["current"] != "1.0.0":
                    self.install.rollback()
                pointer = os.readlink(self.base / "current")
                original = getattr(Installation, method)

                def fail(*args, **kwargs):
                    raise OSError("synthetic interruption")

                with patch.object(Installation, method, fail):
                    with self.assertRaises(OSError):
                        if operation == "activate":
                            self.install.activate("2.0.0")
                        else:
                            self.install.rollback()
                self.assertIsNotNone(self.journal()["transaction"])
                # The pointer is never left between releases.
                self.assertEqual(os.readlink(self.base / "current"), pointer)
                self.assertIs(getattr(Installation, method), original)
                status = self.install.status()
                self.assertIsNotNone(status["transaction"])
                self.install.recover()
                journal = self.journal()
                self.assertIsNone(journal["transaction"])
                expected = "2.0.0" if operation == "activate" else "1.0.0"
                self.assertEqual(journal["current"], expected)
                self.assertEqual(os.readlink(self.base / "current"), f"releases/{expected}")
                self.assertFalse(any(p.name.endswith(".partial")
                                     for p in (self.base / "backups").iterdir()))

    def test_old_snapshots_are_pruned_but_the_upgrade_point_is_kept(self):
        self.place("1.0.0")
        self.place("1.1.0")
        clock = iter(range(1_800_000_000, 1_800_000_000 + 100_000, 60))
        install = Installation(self.base, probe=stopped, clock=lambda: next(clock))
        install.activate("1.0.0")
        upgrade = install.activate("1.1.0")["backup"]
        for _ in range(4):
            install.rollback()
            upgrade = install.activate("1.1.0")["backup"]
        backups = sorted(p.name for p in (self.base / "backups").iterdir())
        self.assertLessEqual(len(backups), 6)
        self.assertIn(upgrade, backups)

    def test_archive_exposes_install_commands(self):
        result = subprocess.run(
            [sys.executable, "-I", "-B", str(self.release / services.ARCHIVE), "status",
             "--base", str(self.base)], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["current"], None)
        result = subprocess.run(
            [sys.executable, "-I", "-B", str(self.release / services.ARCHIVE), "activate",
             "9.9.9", "--base", str(self.base)], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 2)
        self.assertIn("not staged", result.stderr)


if __name__ == "__main__":
    unittest.main()
