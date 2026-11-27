"""The container entrypoint: environment, first start, conversion, backups, supervisor.

Offline: no network, no Docker; children are fake processes or short Python scripts.
"""

import contextlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock
from unittest.mock import patch

from lights_can_communicate_v2_core import __version__, cli, container
from lights_can_communicate_v2_core.config import ConfigError, load_config

ENV = {"CORE_ADDRESS": "192.168.10.40", "TZ": "America/Toronto",
       "STUDIO_HOSTS": "192.168.10.10, NAS.local"}


class EnvironmentTests(unittest.TestCase):
    def test_values_are_parsed(self):
        env = container.parse_environment(ENV)
        self.assertEqual(env.core_address, "192.168.10.40")
        self.assertEqual(env.timezone, "America/Toronto")
        self.assertEqual(env.studio_hosts, ("192.168.10.10", "nas.local"))

    def test_defaults(self):
        env = container.parse_environment({"CORE_ADDRESS": "10.0.0.5", "TZ": " ",
                                           "STUDIO_HOSTS": "nas.local"})
        self.assertEqual((env.timezone, env.studio_hosts), ("Etc/UTC", ("nas.local",)))

    def test_studio_hosts_are_required(self):
        # Light Studio is reached through the NAS's published port only.
        for value in ("", " , "):
            with self.subTest(value=value), self.assertRaises(ConfigError) as caught:
                container.parse_environment({**ENV, "STUDIO_HOSTS": value})
            self.assertIn("STUDIO_HOSTS", str(caught.exception))

    def test_invalid_values_are_rejected_without_echo(self):
        for change in ({"CORE_ADDRESS": ""}, {"CORE_ADDRESS": "8.8.8.8"},
                       {"CORE_ADDRESS": "0.0.0.0"}, {"CORE_ADDRESS": "PRIVATE"},
                       {"CORE_ADDRESS": "224.0.0.1"}, {"TZ": "Mars/PRIVATE"},
                       {"TZ": "../PRIVATE"}, {"STUDIO_HOSTS": "bad_PRIVATE"},
                       {"STUDIO_HOSTS": ",".join(f"h{n}.local" for n in range(9))}):
            with self.subTest(change=change):
                with self.assertRaises(ConfigError) as caught:
                    container.parse_environment({**ENV, **change})
                self.assertNotIn("PRIVATE", str(caught.exception))
        with self.assertRaises(ConfigError):
            container.parse_environment({})


class DataFolderCase(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory(prefix="tmp_container_data_")
        self.addCleanup(temp.cleanup)
        self.data = Path(temp.name).resolve() / "data"
        self.config_path = self.data / "core.json"
        patcher = patch.object(container, "_log")
        patcher.start()
        self.addCleanup(patcher.stop)

    def prepare(self, **environ):
        with contextlib.redirect_stderr(io.StringIO()):
            return container.prepare(self.data, {**ENV, **environ})

    def values(self):
        return json.loads(self.config_path.read_text())


class FirstStartTests(DataFolderCase):
    def test_empty_folder_gets_a_private_configuration_and_identity(self):
        config = self.prepare()
        value = self.values()
        self.assertEqual(value["version"], 2)
        self.assertEqual(value["state_dir"], "state")
        gateway = value["gateway"]
        self.assertEqual((gateway["lan_ip"], gateway["timezone"]),
                         ("192.168.10.40", "America/Toronto"))
        first = int(gateway["mac"][:2], 16)
        self.assertEqual(first & 0b11, 0b10)  # locally administered, unicast
        self.assertRegex(gateway["mac"], r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$")
        # No lights and no Hue bridge: the virtual lights 3-6 belong to migrated Hue lights.
        self.assertTrue(gateway["live_output"] and gateway["gradient"])
        self.assertFalse(any(gateway[flag] for flag in ("hue_strip", "hue_play", "entertainment")))
        self.assertEqual(value["adapter"]["power_authority"], "owner")
        self.assertEqual(value["container"], {"core_address": "172.30.250.3",
                                              "studio_hosts": ["192.168.10.10", "nas.local"]})
        self.assertEqual(config.gateway.adapter_url, "http://172.30.250.3:19880")
        self.assertEqual(config.setup.port, 19881)
        self.assertEqual(config.state_dir, (self.data / "state").resolve())
        self.assertEqual((self.data / "version").read_text().strip(), __version__)
        self.assertFalse((self.data / "backups").exists())
        registry = json.loads((self.data / "state" / "lights.json").read_text())
        self.assertEqual(registry, {"version": 1, "lights": []})

    def test_tokens_have_the_credential_format_and_private_modes(self):
        config = self.prepare()
        adapter = cli._credential(config.token_file)
        owner = cli._credential(config.power_owner_token_file)
        self.assertNotEqual(adapter, owner)
        # The gateway reads the adapter token from the state directory it serves.
        self.assertEqual(config.token_file, config.state_dir / "adapter-token")
        if os.name == "posix":
            for path in (self.config_path, config.token_file, config.power_owner_token_file):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(config.state_dir.stat().st_mode), 0o700)

    def test_a_fresh_install_passes_the_adapter_check_with_no_lights(self):
        self.prepare()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["adapter", "--check", "--config", str(self.config_path)])
        self.assertEqual(code, 0)
        result = json.loads(out.getvalue())
        self.assertEqual((result["targets"], result["native_hue"]), (0, False))

    def test_the_next_start_keeps_identity_and_tokens(self):
        config = self.prepare()
        before = {path: path.read_bytes() for path in (
            config.token_file, config.power_owner_token_file, self.data / "version")}
        mac = self.values()["gateway"]["mac"]
        self.prepare()
        self.assertEqual(self.values()["gateway"]["mac"], mac)
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)
        self.assertFalse((self.data / "backups").exists())

    def test_every_start_applies_the_project_file_values(self):
        self.prepare()
        mac = self.values()["gateway"]["mac"]
        self.prepare(CORE_ADDRESS="192.168.10.41", TZ="Etc/UTC", STUDIO_HOSTS="nas.local")
        value = self.values()
        self.assertEqual(value["gateway"]["lan_ip"], "192.168.10.41")
        self.assertEqual(value["gateway"]["timezone"], "Etc/UTC")
        self.assertEqual(value["gateway"]["mac"], mac)
        self.assertEqual(value["container"]["studio_hosts"], ["nas.local"])

    def test_an_interrupted_first_start_repeats_safely(self):
        config = self.prepare()
        token = config.token_file.read_bytes()
        self.config_path.unlink()
        (self.data / "version").unlink()
        self.prepare()
        self.assertEqual(config.token_file.read_bytes(), token)


V1 = {
    "version": 1,
    "state_dir": "state",
    "adapter": {
        "targets_file": "state/live-targets.json",
        "token_file": "state/adapter-token",
        "power_owner_token_file": "state/power-owner-token",
        "port": 19880, "gradient": True,
        "hue_config_file": "hue.json",
        "native_profile_file": "native-hue-profile.json",
        "hue_play": True,
        "hue_stream_config_file": "hue-stream.json",
        "power_authority": "homekit",
    },
    "setup": {"port": 19881},
    "gateway": {
        "lan_ip": "192.168.10.50", "mac": "02:00:00:00:00:01",
        "container_name": "lights-can-communicate-v2-gateway",
        "image": "diyhue/core@sha256:" + "0" * 64,
        "live_output": True, "hue_strip": True, "hue_play": True,
        "entertainment": True, "gradient": True, "timezone": "America/Toronto",
    },
    "container": {
        "lan": {"parent": "ovs_eth0", "subnet": "192.168.10.0/24",
                "router": "192.168.10.1", "adapter_ip": "192.168.10.51"},
        "core_subnet": "172.30.250.0/29",
        "studio_hosts": ["192.168.10.10"],
    },
}


class VersionOneTests(DataFolderCase):
    def seed(self, value=V1):
        state = self.data / "state"
        state.mkdir(parents=True)
        for name, content in (("adapter-token", "a" * 64), ("power-owner-token", "b" * 64)):
            (state / name).write_text(content + "\n")
            (state / name).chmod(0o600)
        (state / "live-targets.json").write_text("{}")
        (self.data / "hue.json").write_text("{}")
        self.config_path.write_text(json.dumps(value))

    def test_conversion_keeps_the_identity_tokens_and_every_other_value(self):
        self.seed()
        before = {name: (self.data / "state" / name).read_bytes()
                  for name in ("adapter-token", "power-owner-token")}
        config = self.prepare(CORE_ADDRESS="192.168.10.40")
        value = self.values()
        self.assertEqual(value["version"], 2)
        self.assertEqual(value["gateway"]["mac"], "02:00:00:00:00:01")
        self.assertEqual(value["gateway"]["lan_ip"], "192.168.10.40")
        self.assertEqual(value["adapter"], V1["adapter"])
        self.assertEqual(value["setup"], V1["setup"])
        self.assertEqual(value["container"], {"core_address": "172.30.250.3",
                                              "studio_hosts": ["192.168.10.10", "nas.local"]})
        for flag in ("live_output", "hue_strip", "hue_play", "entertainment", "gradient"):
            self.assertTrue(value["gateway"][flag])
        self.assertNotIn("image", value["gateway"])
        self.assertNotIn("container_name", value["gateway"])
        self.assertEqual(config.token_file, self.data / "state" / "adapter-token")
        for name, content in before.items():
            self.assertEqual((self.data / "state" / name).read_bytes(), content)
        self.assertEqual(config.power_authority, "homekit")
        self.assertTrue(config.hue_play)

    def test_the_first_start_after_conversion_is_backed_up_in_its_old_form(self):
        self.seed()
        self.prepare()
        (backup,) = (self.data / "backups").iterdir()
        self.assertTrue(backup.name.endswith(f"-unknown-{__version__}"))
        self.assertEqual(json.loads((backup / "core.json").read_text()), V1)
        self.assertTrue((backup / "state" / "adapter-token").is_file())

    def test_a_custom_core_subnet_moves_to_the_fixed_core_address(self):
        # The project file assigns 172.30.250.3; an old subnet or bind must not
        # leave the adapter on an address the container does not have.
        value = json.loads(json.dumps(V1))
        value["container"]["core_subnet"] = "172.30.251.0/29"
        value["adapter"]["bind"] = "172.30.251.3"
        value["setup"]["bind"] = "0.0.0.0"
        self.seed(value)
        config = self.prepare()
        self.assertEqual((config.container.core_address, config.bind),
                         ("172.30.250.3", "172.30.250.3"))
        self.assertNotIn("bind", self.values()["setup"])

    def test_an_adapter_url_on_the_old_core_network_follows_the_adapter(self):
        value = json.loads(json.dumps(V1))
        value["container"]["core_subnet"] = "172.30.251.0/29"
        value["adapter"]["bind"] = "172.30.251.3"
        value["gateway"]["adapter_url"] = "http://172.30.251.3:19880"
        self.seed(value)
        config = self.prepare()
        self.assertEqual(config.gateway.environment()["PROBE_ADAPTER_URL"],
                         "http://172.30.250.3:19880")

    def test_an_adapter_url_with_the_old_compose_name_follows_the_adapter(self):
        for url in ("http://adapter:19880", "http://adapter.:19880",
                    "http://lights-can-communicate-v2-adapter:19880"):
            with self.subTest(url=url):
                shutil.rmtree(self.data, ignore_errors=True)
                value = json.loads(json.dumps(V1))
                value["gateway"]["adapter_url"] = url
                self.seed(value)
                self.assertEqual(self.prepare().gateway.adapter_url,
                                 "http://172.30.250.3:19880")

    def test_an_unrelated_adapter_url_stays(self):
        value = json.loads(json.dumps(V1))
        value["gateway"]["adapter_url"] = "http://192.168.10.77:19880"
        self.seed(value)
        self.prepare()
        self.assertEqual(self.values()["gateway"]["adapter_url"], "http://192.168.10.77:19880")

    def test_an_unsupported_version_is_refused(self):
        self.seed({**V1, "version": 3})
        with self.assertRaises(ConfigError):
            self.prepare()

    def test_conversion_is_idempotent(self):
        self.seed()
        self.prepare()
        first = self.config_path.read_bytes()
        self.prepare()
        self.assertEqual(self.config_path.read_bytes(), first)
        self.assertEqual(load_config(self.config_path).gateway.mac, "02:00:00:00:00:01")


class BackupTests(DataFolderCase):
    def setUp(self):
        super().setUp()
        self.prepare()
        self.state = self.data / "state"
        (self.state / "desk-layout.json").write_text('{"version": 8}')
        (self.state / "small.jsonl").write_text("row\n")
        (self.state / "requests.jsonl").write_bytes(b"x" * (container.LOG_LIMIT + 1))

    def stamp(self, second):
        return datetime(2026, 11, 26, 12, 0, second, tzinfo=timezone.utc)

    def test_a_version_change_copies_configuration_tokens_and_state(self):
        (self.data / "version").write_text("0.0.9\n")
        folder = container.version_change(self.data, "0.1.0", self.stamp(1))
        self.assertEqual(folder.name, "20261126T120001Z-0.0.9-0.1.0")
        self.assertEqual(json.loads((folder / "core.json").read_text()), self.values())
        self.assertEqual((folder / "state" / "desk-layout.json").read_text(), '{"version": 8}')
        self.assertTrue((folder / "state" / "adapter-token").is_file())
        # Large append-only logs are history, not state.
        self.assertTrue((folder / "state" / "small.jsonl").is_file())
        self.assertFalse((folder / "state" / "requests.jsonl").exists())
        self.assertEqual((self.data / "version").read_text().strip(), "0.1.0")
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
            self.assertEqual(
                stat.S_IMODE((folder / "state" / "adapter-token").stat().st_mode), 0o600)

    def test_tokens_outside_the_state_folder_are_copied(self):
        value = self.values()
        value["adapter"]["token_file"] = "adapter-token"
        self.config_path.write_text(json.dumps(value))
        (self.data / "adapter-token").write_text("a" * 64 + "\n")
        (self.data / "version").write_text("0.0.9\n")
        folder = container.version_change(self.data, "0.1.0", self.stamp(1))
        self.assertTrue((folder / "adapter-token").is_file())

    def test_the_same_version_makes_no_backup(self):
        self.assertIsNone(container.version_change(self.data))
        self.assertFalse((self.data / "backups").exists())

    def test_a_missing_version_file_backs_up_as_unknown(self):
        (self.data / "version").unlink()
        folder = container.version_change(self.data, "0.1.0", self.stamp(2))
        self.assertEqual(folder.name, "20261126T120002Z-unknown-0.1.0")

    def test_a_hostile_version_file_cannot_name_a_path(self):
        (self.data / "version").write_text("../../escape\n")
        folder = container.version_change(self.data, "0.1.0", self.stamp(3))
        self.assertEqual(folder.parent, self.data / "backups")
        self.assertIn("-unknown-", folder.name)

    def test_the_newest_five_backups_stay(self):
        for second in range(8):
            (self.data / "version").write_text(f"0.0.{second}\n")
            container.version_change(self.data, "0.1.0", self.stamp(second))
        names = sorted(path.name for path in (self.data / "backups").iterdir())
        self.assertEqual(len(names), container.KEEP_BACKUPS)
        self.assertEqual([name[:16] for name in names],
                         [f"20261126T1200{s:02d}Z" for s in range(3, 8)])

    def test_two_backups_in_one_second_do_not_collide(self):
        for number in range(2):
            (self.data / "version").write_text(f"0.0.{number}\n")
            container.version_change(self.data, "0.0.9", self.stamp(1))
        self.assertEqual(len(list((self.data / "backups").iterdir())), 2)

    def test_a_partial_backup_is_removed_on_failure_and_never_listed(self):
        (self.data / "version").write_text("0.0.9\n")
        (self.state / "unreadable.json").write_text("{}")
        original = container.shutil.copytree

        def broken(*args, **kwargs):
            raise OSError("disk full")

        container.shutil.copytree = broken
        try:
            with self.assertRaises(OSError):
                container.version_change(self.data, "0.1.0", self.stamp(1))
        finally:
            container.shutil.copytree = original
        self.assertEqual(list((self.data / "backups").iterdir()), [])
        # The version is not recorded, so the next start tries again.
        self.assertEqual((self.data / "version").read_text().strip(), "0.0.9")


class FakeProcess:
    def __init__(self, clock, lifetime=None, ignores_terminate=False):
        self.clock, self.ended = clock, None
        self.lifetime, self.ignores = lifetime, ignores_terminate
        self.started = clock()
        self.signals = []

    def poll(self):
        if self.ended is None and self.lifetime is not None and (
                self.clock() - self.started >= self.lifetime):
            self.ended = 1
        return self.ended

    def terminate(self):
        self.signals.append("TERM")
        if not self.ignores:
            self.ended = 0

    def kill(self):
        self.signals.append("KILL")
        self.ended = -9

    def wait(self, timeout=None):
        return self.ended


class FakeTime:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class SupervisorTests(unittest.TestCase):
    def build(self, lifetimes, names=("gateway",), **options):
        clock = FakeTime()
        started = []

        def popen(argv, env=None, cwd=None):
            lifetime = lifetimes(argv[0], len(started))
            process = FakeProcess(clock, *lifetime)
            started.append((argv[0], clock(), process))
            return process

        specs = [container.ChildSpec(name, (name,), {}) for name in names]
        supervisor = container.Supervisor(specs, popen=popen, clock=clock, sleep=clock.sleep,
                                          log=lambda message: None, **options)
        return supervisor, clock, started

    def advance(self, supervisor, clock, seconds, step=0.1):
        end = clock.now + seconds
        while clock.now < end:
            supervisor.tick()
            clock.sleep(step)

    def gaps(self, started):
        times = [at for _, at, _ in started]
        return [b - a for a, b in zip(times, times[1:])]

    def assertDelays(self, gaps, delays):
        # Each gap is the delay plus the tick that notices the exit.
        for gap, delay in zip(gaps, delays):
            self.assertAlmostEqual(gap, delay, delta=0.25)

    def test_a_failing_child_restarts_with_doubling_delays_up_to_the_cap(self):
        supervisor, clock, started = self.build(lambda name, n: (0.0,))
        self.advance(supervisor, clock, 200)
        gaps = self.gaps(started)
        self.assertDelays(gaps, [1, 2, 4, 8, 16, 30, 30, 30])
        self.assertEqual(started[0][1], 0)

    def test_a_child_that_ran_long_enough_restarts_quickly_again(self):
        def lifetime(name, number):
            return (70.0,) if number == 3 else (0.0,)

        supervisor, clock, started = self.build(lifetime)
        self.advance(supervisor, clock, 160)
        # Three failures wait 1, 2 and 4 s; the fourth run lasts 70 s, so its
        # restart waits 1 s instead of 8 s, and a new failure series doubles again.
        self.assertDelays(self.gaps(started), [1, 2, 4, 70 + 1, 2, 4])

    def test_children_restart_independently(self):
        supervisor, clock, started = self.build(
            lambda name, n: (0.0,) if name == "gateway" else (None,),
            names=("gateway", "adapter", "studio"))
        self.advance(supervisor, clock, 10)
        counts = {name: sum(1 for item in started if item[0] == name)
                  for name in ("gateway", "adapter", "studio")}
        self.assertEqual(counts["adapter"], 1)
        self.assertEqual(counts["studio"], 1)
        self.assertGreater(counts["gateway"], 2)

    def test_a_command_that_cannot_start_is_retried(self):
        clock, attempts = FakeTime(), []

        def popen(argv, env=None, cwd=None):
            attempts.append(clock())
            raise FileNotFoundError("python3")

        supervisor = container.Supervisor(
            [container.ChildSpec("gateway", ("x",), {})], popen=popen, clock=clock,
            sleep=clock.sleep, log=lambda message: None)
        self.advance(supervisor, clock, 12)
        self.assertEqual([round(a) for a in attempts], [0, 1, 3, 7])

    def test_shutdown_terminates_every_child_and_waits_for_them(self):
        supervisor, clock, started = self.build(
            lambda name, n: (None,), names=("gateway", "adapter", "studio"))
        supervisor.tick()
        supervisor.stop()
        supervisor.shutdown()
        self.assertEqual([item[2].signals for item in started], [["TERM"]] * 3)
        self.assertLess(clock.now, 1)

    def test_a_child_that_ignores_terminate_is_killed_after_the_grace_period(self):
        supervisor, clock, started = self.build(
            lambda name, n: (None, name == "adapter"), names=("gateway", "adapter"))
        supervisor.tick()
        supervisor.shutdown()
        by_name = {name: process for name, _, process in started}
        self.assertEqual(by_name["gateway"].signals, ["TERM"])
        self.assertEqual(by_name["adapter"].signals, ["TERM", "KILL"])
        self.assertGreaterEqual(clock.now, container.SHUTDOWN_GRACE)
        self.assertLess(clock.now, container.SHUTDOWN_GRACE + 1)

    def test_run_stops_when_asked_and_returns_zero(self):
        supervisor, clock, started = self.build(lambda name, n: (None,))
        ticks = []

        def sleep(seconds):
            ticks.append(seconds)
            clock.sleep(seconds)
            if len(ticks) == 3:
                supervisor.stop()

        supervisor.sleep = sleep
        self.assertEqual(supervisor.run(handle_signals=False), 0)
        self.assertEqual(started[0][2].signals, ["TERM"])


@unittest.skipUnless(os.name == "posix", "Signals need POSIX")
class RealProcessTests(unittest.TestCase):
    """The real supervisor loop with real, tiny child processes."""

    def test_sigterm_stops_the_children_and_exits_zero(self):
        script = (
            "import sys, time\n"
            "from lights_can_communicate_v2_core import container\n"
            "specs = [container.ChildSpec(n, (sys.executable, '-c', 'import time; time.sleep(60)'),"
            " {}) for n in ('gateway', 'adapter', 'studio')]\n"
            "supervisor = container.Supervisor(\n"
            "    specs, poll=0.05, log=lambda m: print(m, flush=True))\n"
            "raise SystemExit(supervisor.run())\n"
        )
        root = Path(__file__).resolve().parents[1]
        process = subprocess.Popen([sys.executable, "-B", "-c", script], cwd=root,
                                   stdout=subprocess.PIPE, text=True)
        try:
            lines = [process.stdout.readline() for _ in range(3)]
            self.assertEqual(sorted(line.strip() for line in lines),
                             ["started adapter", "started gateway", "started studio"])
            time.sleep(0.2)
            process.terminate()
            self.assertEqual(process.wait(timeout=10), 0)
            self.assertIn("stopping", process.stdout.read())
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stdout.close()


class ReviewCaseTests(BackupTests):
    def test_a_configured_state_folder_is_the_one_backed_up(self):
        # A state_dir other than state/ is still the folder the runtime uses.
        os.replace(self.data / "state", self.data / "durable")
        value = self.values()
        value["state_dir"] = "durable"
        for key in ("token_file", "power_owner_token_file"):
            value["adapter"][key] = value["adapter"][key].replace("state/", "durable/")
        self.config_path.write_text(json.dumps(value))
        (self.data / "version").write_text("0.0.9\n")
        folder = container.version_change(self.data, "0.1.0", self.stamp(1))
        self.assertTrue((folder / "durable" / "desk-layout.json").is_file())
        self.assertTrue((folder / "durable" / "adapter-token").is_file())

    def test_a_state_folder_outside_the_data_folder_is_refused(self):
        for state_dir in ("../elsewhere", ".", "backups", "backups/inner"):
            with self.subTest(state_dir=state_dir):
                value = self.values()
                value["state_dir"] = state_dir
                self.config_path.write_text(json.dumps(value))
                (self.data / "version").write_text("0.0.9\n")
                with self.assertRaises(ConfigError):
                    container.version_change(self.data, "0.1.0", self.stamp(1))
                self.assertEqual((self.data / "version").read_text(), "0.0.9\n")
                self.assertFalse((self.data / "backups").exists())

    def test_a_new_token_is_complete_and_no_partial_file_stays(self):
        token = self.data / "fresh" / "adapter-token"
        container._new_token(token)
        self.assertEqual(len(token.read_text().strip()), 64)
        container._new_token(token)
        self.assertEqual(sorted(p.name for p in token.parent.iterdir()), ["adapter-token"])

    def test_an_unassigned_core_address_stops_with_a_clear_message(self):
        with self.assertRaises(ConfigError) as caught:
            container._require_core_address("172.30.250.3")
        self.assertIn("ipv4_address", str(caught.exception))
        container._require_core_address("127.0.0.1")

    def test_a_relative_data_folder_gives_the_children_absolute_paths(self):
        seen = []

        class Recorder:
            def __init__(self, specs):
                seen.extend(specs)

            def run(self):
                return 0

        relative = os.path.relpath(self.data)
        with mock.patch.object(container, "_require_core_address"):
            container.run(relative, {**ENV}, supervisor=Recorder)
        for spec in seen[1:]:
            self.assertTrue(Path(spec.argv[-1]).is_absolute())


class ChildCommandTests(DataFolderCase):
    def test_commands_and_environment_of_the_three_children(self):
        config = self.prepare()
        specs = {spec.name: spec for spec in container.children(
            config, self.config_path, archive="/app/core.pyz", probe="/probe",
            gateway_dir=self.data / "missing")}
        self.assertEqual(list(specs), ["gateway", "adapter", "studio"])
        gateway = specs["gateway"]
        self.assertEqual(gateway.argv, ("python3", "-u", "/probe/bootstrap.py", "--docker"))
        # diyHue keeps its state in the data folder: the image has no mount to share.
        self.assertEqual(gateway.env["CONFIG_PATH"], str(config.state_dir))
        self.assertEqual(gateway.env["IP"], "192.168.10.40")
        self.assertEqual(gateway.env["TZ"], "America/Toronto")
        self.assertEqual(gateway.env["PROBE_ADAPTER_URL"], "http://172.30.250.3:19880")
        self.assertEqual(gateway.env["PROBE_LIVE_OUTPUT"], "true")
        self.assertEqual(gateway.env["PROBE_HUE_STRIP"], "false")
        self.assertIsNone(gateway.cwd)
        for name, command in (("adapter", "adapter"), ("studio", "setup")):
            self.assertEqual(specs[name].argv, (
                "python3", "-I", "-B", "-u", "/app/core.pyz", command,
                "--config", str(self.config_path)))
            self.assertEqual(specs[name].cwd, str(config.state_dir))
        for spec in specs.values():
            self.assertNotIn(cli._credential(config.token_file).decode(),
                             " ".join(spec.argv))


if __name__ == "__main__":
    unittest.main()
