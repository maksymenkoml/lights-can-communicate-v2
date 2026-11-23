"""Offline contracts for staging, activation, rollback and interrupted transactions."""

import json
import os
import shlex
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import nas_profile

from lights_can_communicate_v2_core import __version__, services
from lights_can_communicate_v2_core.dist import STATE_SCHEMAS, build_release
from lights_can_communicate_v2_core.install import Installation, InstallError, verify

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge"))
from desk_layout import LAYOUT_VERSION, validate_layout  # noqa: E402

TOKEN = "a" * 48
# A later release that changes the stored layout format.
NEXT_LAYOUT_SCHEMA = {**STATE_SCHEMAS, "desk_layout": STATE_SCHEMAS["desk_layout"] + 1}


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
        (self.base / "config" / "targets.json").write_text(json.dumps({"targets": [
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
            "adapter": {"targets_file": str(self.base / "config" / "targets.json"),
                        "token_file": str(self.state / "adapter-token"),
                        "power_owner_token_file": str(self.state / "owner-token"),
                        "port": 19990},
            **nas_profile.sections(),
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
        self.assertEqual(compatibility["core_api"], {"homekit": 2})
        self.assertEqual(compatibility["state"], STATE_SCHEMAS)
        plugin = compatibility["plugin"]
        self.assertIn(plugin["file"], self.manifest["files"])
        with TemporaryDirectory(prefix="tmp_install_again_") as again:
            second = build_release(Path(again) / "release")
        self.assertEqual(second["files"], self.manifest["files"])
        # The declared layout schema is the one the gateway validates.
        self.assertEqual(STATE_SCHEMAS["desk_layout"], LAYOUT_VERSION)
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
        compose = json.loads((staged / "services" / "compose.json").read_text())
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
        self.config["adapter"]["targets_file"] = str(self.base / "config" / "missing.json")
        (self.base / "config" / "core.json").write_text(json.dumps(self.config))
        with self.assertRaisesRegex(InstallError, "does not validate"):
            self.install.stage(self.release)
        self.assertFalse((self.base / "releases" / __version__).exists())
        self.assertFalse((self.base / "releases" / (__version__ + ".partial")).exists())

    def test_a_different_build_of_an_installed_version_is_refused(self):
        for change in ("files", "compatibility"):
            with self.subTest(change=change):
                shutil.rmtree(self.base / "releases", ignore_errors=True)
                self.place(__version__)
                path = self.base / "releases" / __version__ / "manifest.json"
                manifest = json.loads(path.read_text())
                if change == "files":
                    manifest["files"]["gateway/bootstrap.py"] = "0" * 64
                else:
                    manifest["compatibility"]["state"] = {**STATE_SCHEMAS, "desk_layout": 9}
                path.write_text(json.dumps(manifest))
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

    def test_activation_revalidates_the_configuration_changed_after_staging(self):
        self.install.stage(self.release)
        self.config["adapter"]["targets_file"] = str(self.base / "config" / "missing.json")
        (self.base / "config" / "core.json").write_text(json.dumps(self.config))
        with self.assertRaisesRegex(InstallError, "does not validate"):
            self.install.activate(__version__)
        self.assertFalse((self.base / "current").exists())
        self.assertFalse((self.base / "install.json").exists())

    def rendered_ip(self, version=__version__):
        compose = self.base / "releases" / version / "services" / "compose.json"
        return json.loads(compose.read_text())["services"]["gateway"]["environment"]["IP"]

    def change_gateway_address(self):
        self.config["gateway"]["lan_ip"] = "192.168.99.20"
        (self.base / "config" / "core.json").write_text(json.dumps(self.config))

    def test_staging_again_renders_a_changed_configuration(self):
        self.install.stage(self.release)
        services_dir = self.base / "releases" / __version__ / "services"
        self.assertTrue((services_dir / "config.sha256").is_file())
        # Reformatting the same configuration is not a change.
        (self.base / "config" / "core.json").write_text(json.dumps(self.config, indent=4))
        self.assertEqual(self.install.stage(self.release),
                         {"version": __version__, "staged": False, "rendered": False})
        self.change_gateway_address()
        self.assertEqual(self.install.stage(self.release),
                         {"version": __version__, "staged": False, "rendered": True})
        self.assertEqual(self.rendered_ip(), "192.168.99.20")
        verify(self.base / "releases" / __version__)
        self.assertEqual([p.name for p in (self.base / "releases").iterdir()], [__version__])

    def test_staging_again_validates_the_changed_configuration(self):
        self.install.stage(self.release)
        self.config["gateway"]["hue_strip"] = False
        self.config["gateway"]["entertainment"] = True
        (self.base / "config" / "core.json").write_text(json.dumps(self.config))
        with self.assertRaisesRegex(InstallError, "does not validate"):
            self.install.stage(self.release)
        self.assertEqual(self.rendered_ip(), "192.168.99.10")

    def test_activation_renders_a_configuration_changed_after_staging(self):
        self.install.stage(self.release)
        self.change_gateway_address()
        self.install.activate(__version__)
        self.assertEqual(self.rendered_ip(), "192.168.99.20")

    def test_activating_the_active_release_applies_a_changed_configuration(self):
        self.install.stage(self.release)
        self.install.activate(__version__)
        self.change_gateway_address()
        with self.assertRaisesRegex(InstallError, "activate it again"):
            self.install.stage(self.release)
        busy = Installation(self.base, probe=lambda _: ["gateway"])
        with self.assertRaisesRegex(InstallError, "gateway"):
            busy.activate(__version__)
        self.assertEqual(self.rendered_ip(), "192.168.99.10")
        result = self.install.activate(__version__)
        self.assertEqual((result["changed"], result["rendered"]), (False, True))
        self.assertEqual(result["start"], self.install.start_commands())
        self.assertEqual(self.rendered_ip(), "192.168.99.20")
        self.assertEqual(self.install.activate(__version__),
                         {"active": __version__, "changed": False, "rendered": False})

    def test_a_release_rendered_before_digests_is_not_treated_as_stale(self):
        self.install.stage(self.release)
        services_dir = self.base / "releases" / __version__ / "services"
        (services_dir / "config.sha256").unlink()
        self.install.activate(__version__)
        self.change_gateway_address()
        # Active: its files are left alone and no digest is claimed for them.
        self.assertEqual(self.install.stage(self.release)["rendered"], False)
        self.assertFalse((services_dir / "config.sha256").exists())
        self.assertFalse(self.install.activate(__version__)["changed"])
        self.assertEqual(self.rendered_ip(), "192.168.99.10")
        # Inactive: staging again renders it and records the digest.
        (self.base / "current").unlink()
        self.assertEqual(self.install.stage(self.release)["rendered"], True)
        self.assertEqual(self.rendered_ip(), "192.168.99.20")
        self.assertTrue((services_dir / "config.sha256").is_file())

    def test_a_stale_release_of_another_version_is_not_rendered_here(self):
        target = self.place("1.0.0")
        (target / "services").mkdir()
        (target / "services" / "config.sha256").write_text("0" * 64 + "\n")
        with self.assertRaisesRegex(InstallError, "stage this release again"):
            self.install.activate("1.0.0")
        self.assertFalse((self.base / "current").exists())

    def test_recovery_waits_until_components_are_stopped(self):
        self.place("1.0.0")
        with patch.object(Installation, "_point", side_effect=OSError("interrupted")):
            with self.assertRaises(OSError):
                self.install.activate("1.0.0")
        busy = Installation(self.base, probe=lambda _: ["gateway"])
        with self.assertRaisesRegex(InstallError, "gateway"):
            busy.recover()
        self.assertFalse((self.base / "current").exists())
        self.assertIsNotNone(self.journal()["transaction"])
        self.install.recover()
        self.assertEqual(os.readlink(self.base / "current"), "releases/1.0.0")

    def test_start_commands_quote_installation_paths(self):
        install = Installation(self.base / "it's; touch owned", probe=stopped)
        for command in install.start_commands():
            self.assertNotIn("touch", shlex.split(command)[0])
            self.assertFalse(any(part == "touch" for part in shlex.split(command)))

    def test_start_commands_start_the_whole_compose_project(self):
        commands = self.install.start_commands()
        self.assertEqual(shlex.split(commands[0])[-3:], ["up", "-d", "--force-recreate"])
        self.assertTrue(shlex.split(commands[0])[3].endswith("/current/services/compose.json"))
        self.assertEqual(shlex.split(commands[1])[:3],
                         ["docker", "exec", "lights-can-communicate-v2-adapter"])
        self.assertEqual(len(commands), 2)

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
        self.place("2.0.0", state=NEXT_LAYOUT_SCHEMA)
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
        self.place("2.0.0", state=NEXT_LAYOUT_SCHEMA)
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
