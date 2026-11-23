"""Interrupt startup after a real video worker begins, without hardware IO."""

import io
import json
import sys
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge"))
import live_adapter as adapter  # noqa: E402
import nas_profile  # noqa: E402
from entertainment_stream import VideoLease  # noqa: E402
from registry_files import legacy_targets, write_registry  # noqa: E402
from test_live_adapter import targets  # noqa: E402

from lights_can_communicate_v2_core import cli  # noqa: E402
from lights_can_communicate_v2_core.light_registry import LightRegistry, RegistryError  # noqa: E402


class RegistryCheckTests(unittest.TestCase):
    def test_check_does_not_write_registry(self):
        with TemporaryDirectory(prefix="tmp_adapter_check_") as directory:
            root = Path(directory).resolve()
            state = root / "state"
            state.mkdir()
            (root / "targets.json").write_text(json.dumps({"targets": legacy_targets(targets())}))
            for name, content in (("token", "a" * 48), ("owner", "b" * 64)):
                (state / name).write_text(content)
                (state / name).chmod(0o600)
            (root / "core.json").write_text(json.dumps({
                "version": 1, "state_dir": "state",
                "adapter": {"targets_file": "targets.json", "token_file": "state/token",
                            "power_owner_token_file": "state/owner"},
                **nas_profile.sections(),
            }))
            before = sorted(item.name for item in root.rglob("*"))
            output, errors = io.StringIO(), io.StringIO()
            with redirect_stdout(output), redirect_stderr(errors):
                code = cli.main(["--config", str(root / "core.json"), "--check"])
            self.assertEqual(code, 0, errors.getvalue())
            self.assertEqual(json.loads(output.getvalue())["targets"], 2)
            self.assertEqual(json.loads(output.getvalue())["profile"], "govee-only")
            self.assertEqual(sorted(item.name for item in root.rglob("*")), before)
            self.assertFalse((state / "lights.json").exists())


class AdapterLifecycleTests(unittest.TestCase):
    def test_startup_interrupt_closes_server_and_joins_already_started_video(self):
        started, stopped = threading.Event(), threading.Event()
        events, workers = [], []

        class Server:
            def __init__(self, *args):
                self.closed = False
                self.shutdown_calls = 0
                events.append(self)

            def shutdown(self):
                self.shutdown_calls += 1

            def server_close(self):
                self.closed = True

        class Proxy:
            def __init__(self, *args, **kwargs):
                pass

            def run(self, stop):
                stop.wait()

            def process_once(self):
                pass

            def govee_allowed(self):
                return True

            def included(self):
                return True

        class Discovery:
            def __init__(self, *args, **kwargs):
                pass

            def run(self, stop):
                stop.wait()

        class Video:
            def __init__(self, controller, relay, proxies, diagnostics=None):
                self.controller = controller
                self.diagnostics = diagnostics
                self.lease = VideoLease()
                self.lock = threading.RLock()

            def active(self):
                return self.lease.active()

            def run(self, stop):
                workers.append((threading.current_thread(), stop))
                started.set()
                stop.wait()
                stopped.set()

        def interrupted_transport(*args, **kwargs):
            self.assertTrue(started.wait(1))
            raise KeyboardInterrupt

        with TemporaryDirectory(prefix="tmp_adapter_startup_") as directory:
            root = Path(directory)
            from test_native_profile import profile_data

            write_registry(root, targets(), profile_data())
            (root / "token").write_text("a" * 48)
            (root / "hue.json").write_text('{"bridge":"192.168.99.2"}')
            (root / "stream.json").write_text('{"bridge":"192.168.99.2"}')
            args = [
                "--registry",
                str(root / "lights.json"),
                "--token-file",
                str(root / "token"),
                "--hue-config",
                str(root / "hue.json"),
                "--hue-play",
                "--hue-stream-config",
                str(root / "stream.json"),
            ]
            try:
                with (
                    patch.object(adapter, "ThreadingHTTPServer", Server),
                    patch.object(adapter, "PinnedHueClient", lambda *a, **kw: object()),
                    patch.object(adapter, "HueStripProxy", Proxy),
                    patch.object(adapter, "HueEntertainmentRelay", lambda *a, **kw: object()),
                    patch.object(adapter, "HueDiscoveryReader", Discovery),
                    patch.object(adapter, "VideoOutput", Video),
                    patch.object(adapter, "GoveeTransport", interrupted_transport),
                ):
                    try:
                        adapter.main(args)
                    except KeyboardInterrupt:
                        pass
                self.assertTrue(events[0].closed, "Startup interrupt leaked the HTTP socket")
                self.assertEqual(
                    events[0].shutdown_calls, 0, "Unstarted HTTP loop cannot be shut down"
                )
                self.assertTrue(stopped.is_set(), "Started video worker was not stopped")
                self.assertFalse(workers[0][0].is_alive())
            finally:
                for worker, stop in workers:
                    stop.set()
                    worker.join(2)

    def test_address_change_is_remembered_in_the_registry_only(self):
        captured = {}

        class Server:
            def __init__(self, *args):
                pass

            def shutdown(self):
                pass

            def server_close(self):
                pass

        def interrupted_transport(*args, **kwargs):
            captured["targets"] = args[0]
            captured["remember"] = kwargs["remember"]
            raise KeyboardInterrupt

        with TemporaryDirectory(prefix="tmp_adapter_remember_") as directory:
            root = Path(directory)
            write_registry(root, targets())
            (root / "token").write_text("a" * 48)
            registry_before = (root / "lights.json").read_bytes()
            with (
                patch.object(adapter, "ThreadingHTTPServer", Server),
                patch.object(adapter, "GoveeTransport", interrupted_transport),
            ):
                adapter.main(["--registry", str(root / "lights.json"),
                              "--token-file", str(root / "token")])
            self.assertEqual((root / "lights.json").read_bytes(), registry_before)
            captured["targets"][0]["ip"] = "192.168.10.99"
            captured["remember"]()
            saved = LightRegistry.load(root / "lights.json").govee_targets()
            self.assertEqual(saved[0]["ip"], "192.168.10.99")
            self.assertEqual(saved[1]["ip"], targets()[1]["ip"])
            self.assertEqual([item.name for item in root.glob("*.tmp")], [])
            with patch.object(LightRegistry, "save", side_effect=RegistryError("x")):
                with self.assertRaises(OSError):
                    captured["remember"]()


if __name__ == "__main__":
    unittest.main()
