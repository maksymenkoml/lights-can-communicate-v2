"""Interrupt startup after a real video worker begins, without hardware IO."""

import json
import sys
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge"))
import live_adapter as adapter  # noqa: E402
from entertainment_stream import VideoLease  # noqa: E402
from test_live_adapter import targets  # noqa: E402


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
            (root / "targets.json").write_text(json.dumps({"targets": targets()}))
            (root / "token").write_text("a" * 48)
            (root / "hue.json").write_text('{"bridge":"192.168.99.2"}')
            (root / "stream.json").write_text('{"bridge":"192.168.99.2"}')
            from test_native_profile import profile_data

            (root / "native.json").write_text(json.dumps(profile_data()))
            args = [
                "--targets",
                str(root / "targets.json"),
                "--token-file",
                str(root / "token"),
                "--hue-config",
                str(root / "hue.json"),
                "--native-profile",
                str(root / "native.json"),
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


if __name__ == "__main__":
    unittest.main()
