import importlib.util
import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

spec = importlib.util.spec_from_file_location(
    "setup_studio", Path(__file__).resolve().parents[1] / "experiments/setup-studio/server.py"
)
studio = importlib.util.module_from_spec(spec)
spec.loader.exec_module(studio)


class StudioTests(unittest.TestCase):
    def test_status_projection_excludes_keys_unselected_devices_and_raw_commands(self):
        result = studio.project_status(
            {
                "hue_key": "secret-key",
                "video": {"active": True, "ready": True, "session": "private-session"},
                "targets": [
                    {
                        "side": "left",
                        "native": {
                            "identity_verified": True,
                            "onOff": 1,
                            "brightness": 51,
                            "secret": "secret-token",
                        },
                    },
                    {"side": "third", "native": {"onOff": 1}},
                ],
                "hue_play": {
                    "hue_center": {
                        "healthy": True,
                        "native_on": True,
                        "native_brightness": 39,
                        "last_payload": {"sensitive": "secret-body"},
                    }
                },
            }
        )
        encoded = json.dumps(result)
        self.assertEqual(result["mode"], "sync")
        self.assertEqual(
            result["lamps"]["govee-left"], {"online": True, "on": True, "brightness": 51}
        )
        for forbidden in ("secret-", "private-session", "third", "last_payload"):
            self.assertNotIn(forbidden, encoded)

    def test_server_serves_only_public_assets_and_rejects_write_routes(self):
        with TemporaryDirectory(prefix="tmp_studio_") as folder:
            server = ThreadingHTTPServer(
                ("127.0.0.1", 0), studio.handler_for(Path(folder) / "missing-token")
            )
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            base = "http://127.0.0.1:" + str(server.server_port)
            try:
                with opener.open(base + "/") as response:
                    self.assertIn(b"Light Studio", response.read())
                for path in (
                    "/.lighting-stream.json",
                    "/server.py",
                    "/../hue-bridge/state/adapter-token",
                ):
                    with self.assertRaises(urllib.error.HTTPError) as err:
                        opener.open(base + path)
                    self.assertEqual(err.exception.code, 404)
                with opener.open(base + "/api/pilot") as response:
                    self.assertFalse(json.load(response)["connected"])
                request = urllib.request.Request(base + "/command", method="POST", data=b"{}")
                with self.assertRaises(urllib.error.HTTPError) as err:
                    opener.open(request)
                self.assertEqual(err.exception.code, 405)
            finally:
                server.shutdown()
                server.server_close()
                thread.join()
