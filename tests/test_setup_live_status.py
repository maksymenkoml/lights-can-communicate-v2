"""Read-only UI status projection and sleep-aware source freshness."""

import json
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

from test_setup_studio import studio


def status():
    lamps = {}
    data = {
        "targets": [],
        "hue": {},
        "video": {"active": False, "ready": False},
        "hue_key": "PRIVATE_CREDENTIAL",
    }
    for side in ("left", "right"):
        data["targets"].append(
            {
                "light": "govee-" + side,
                "device": "PRIVATE_DEVICE",
                "status_age": 0.1,
                "native": {"identity_verified": True, "onOff": 1, "brightness": 65},
            }
        )
        lamps["govee-" + side] = {
            "online": True,
            "on": True,
            "brightness": 65,
            "colors": [[166, 0, 0]] * 7,
            "source": "scene-output",
            "stale": False,
        }
    for identifier in ("hue-monitor", "play-left", "play-center", "play-right"):
        data["hue"][identifier] = {
            "healthy": True,
            "native_age": 0.1,
            "native_on": True,
            "native_brightness": 50 if identifier == "play-center" else 100,
            "native_mode": "normal",
        }
        lamps[identifier] = {
            "online": True,
            "on": True,
            "brightness": 100,
            "colors": [[0, 128, 0]] * (9 if identifier == "hue-monitor" else 1),
            "source": "scene-output",
            "stale": False,
        }
    data["preview"] = {"version": 1, "mode": "scene", "lamps": lamps}
    return data


class SetupLiveStatusTests(unittest.TestCase):
    def test_projection_adds_verified_sync_reason_and_role_blockers(self):
        data = status()
        data["targets"][1]["native"]["onOff"] = 0
        data["video"]["diagnostics"] = {"events": [
            {"state": "waiting", "reason": "native_unavailable", "metrics": {}}]}
        result = studio.project_status(data, source_active=True)
        self.assertEqual(result["sync"]["reason"], "native_unavailable")
        self.assertEqual(result["sync"]["blockers"],
                         [{"role": "govee-right", "code": "native_off"}])
        self.assertNotIn("PRIVATE", json.dumps(result))
        unclassified = studio.project_status(status(), source_active=False)
        self.assertEqual(unclassified["sync"]["state"], "off")
        self.assertEqual(unclassified["sync"]["reason"], "unknown")

    def test_projection_keeps_calibrated_colors_and_native_metadata_without_private_fields(self):
        try:
            result = studio.project_status(status(), source_active=False)
        except TypeError as error:
            self.fail("Live gateway contract missing: " + str(error))
        self.assertEqual(result["sync"]["state"], "off")
        self.assertEqual(result["lamps"]["govee-left"]["colors"][0], [166, 0, 0])
        self.assertEqual(result["lamps"]["play-center"]["colors"], [[0, 128, 0]])
        self.assertEqual(result["lamps"]["play-center"]["brightness"], 50)
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertEqual(len(result["lamps"]), 6)

    def test_lights_enrolled_later_are_projected_with_their_reported_zones(self):
        govee, hue = "device-" + "c" * 32, "device-" + "e" * 32
        data = status()
        data["targets"].append({"light": govee, "device": "PRIVATE_DEVICE", "status_age": 0.1,
                                "tier": "basic", "native": {"identity_verified": True,
                                                            "onOff": 1, "brightness": 40}})
        data["hue"][hue] = dict(data["hue"]["play-left"])
        data["preview"]["lamps"][hue] = {"online": True, "on": True, "brightness": 100,
                                         "colors": [[0, 0, 255], [255, 0, 0]],
                                         "source": "scene-output", "stale": False}
        data["lights"] = [
            {"id": govee, "provider": "govee", "model": "H6008", "zones": 1, "migrated": False},
            {"id": hue, "provider": "hue", "model": "LCX005", "zones": 7, "migrated": False},
            {"id": "device-bad", "provider": "hue", "zones": 1, "migrated": False},
            {"id": "govee-left", "provider": "govee", "model": None, "zones": 7,
             "migrated": True},
        ]
        result = studio.project_status(data, source_active=False)
        self.assertEqual(list(result["lamps"])[:6], ["govee-left", "govee-right", "hue-monitor",
                                                     "play-left", "play-center", "play-right"])
        self.assertEqual(result["lamps"][govee]["brightness"], 40)
        self.assertEqual(len(result["lamps"][hue]["colors"]), 7)
        self.assertEqual([row["id"] for row in result["lights"]], [govee, hue, "govee-left"])
        self.assertNotIn("PRIVATE", json.dumps(result))
        # A later light outside the composed rows is not included in Sync.
        data["composition"] = {"enabled_roles": ["left"], "lights": ["govee-left", govee]}
        result = studio.project_status(data, source_active=False)
        self.assertEqual((result["lamps"][govee]["included"], result["lamps"][hue]["included"]),
                         (True, False))

    def test_monitor_stop_colors_are_resampled_to_all_nine_visual_zones(self):
        data = status()
        data["preview"]["lamps"]["hue-monitor"]["colors"] = [[255, 0, 0], [0, 0, 255]]
        colors = studio.project_status(data, source_active=False)["lamps"]["hue-monitor"]["colors"]
        self.assertEqual(len(colors), 9)
        self.assertEqual(colors[0], [255, 0, 0])
        self.assertEqual(colors[4], [128, 0, 128])
        self.assertEqual(colors[8], [0, 0, 255])

    def test_sync_requires_fresh_ready_output_and_source_idle_is_waiting(self):
        data = status()
        data["video"].update(active=True, ready=True, frame_age=0.1)
        data["preview"]["mode"] = "sync"
        try:
            self.assertEqual(
                studio.project_status(data, source_active=True)["sync"]["state"], "active"
            )
        except TypeError as error:
            self.fail("Source-aware Sync projection missing: " + str(error))
        data["video"]["frame_age"] = 1
        self.assertEqual(
            studio.project_status(data, source_active=True)["sync"]["state"], "waiting"
        )
        data["video"].update(active=False, ready=False, frame_age=None)
        data["preview"]["mode"] = "scene"
        self.assertEqual(
            studio.project_status(data, source_active=True)["sync"]["state"], "waiting"
        )
        self.assertEqual(
            studio.project_status(data, source_active=None)["sync"]["state"], "unknown"
        )
        data["video"].update(active=False, ready=True, native_owned=True, frame_age=1)
        data["preview"]["mode"] = "waiting"
        self.assertEqual(
            studio.project_status(data, source_active=False)["sync"]["state"], "waiting"
        )
        data["video"].update(active=True, ready=False, native_owned=False)
        data["preview"]["mode"] = "foreign"
        self.assertEqual(
            studio.project_status(data, source_active=False)["sync"]["state"], "foreign"
        )

    def test_off_stale_or_malformed_colors_cannot_glow(self):
        data = status()
        data["targets"][0]["native"]["onOff"] = 0
        try:
            result = studio.project_status(data, source_active=False)
        except TypeError as error:
            self.fail("Live gateway missing: " + str(error))
        self.assertFalse(result["lamps"]["govee-left"]["on"])
        self.assertEqual(result["lamps"]["govee-left"]["colors"], [])
        data["preview"]["lamps"]["hue-monitor"]["online"] = False
        self.assertEqual(studio.project_status(data)["lamps"]["hue-monitor"]["colors"], [])
        data["preview"]["lamps"]["hue-monitor"]["online"] = True
        data["preview"]["lamps"]["hue-monitor"]["on"] = False
        self.assertEqual(studio.project_status(data)["lamps"]["hue-monitor"]["colors"], [])
        data["preview"]["lamps"]["hue-monitor"]["on"] = True
        data["targets"][1]["status_age"] = 2
        self.assertIsNone(studio.project_status(data)["lamps"]["govee-right"]["on"])
        for colors in ([[256, 0, 0]], [[True, 0, 0]], [[0, 0, "PRIVATE"]], [[0, 0, 0]] * 10):
            data["preview"]["lamps"]["hue-monitor"]["colors"] = colors
            self.assertEqual(studio.project_status(data)["lamps"]["hue-monitor"]["colors"], [])

    def test_foreign_fallback_ignores_stale_or_unselected_native_metadata(self):
        data = status()
        data["hue"]["hue-monitor"]["native_mode"] = "streaming"
        for change in (
            {"healthy": False},
            {"native_age": None},
            {"native_age": 3},
            {"native_on": None},
        ):
            with self.subTest(change=change):
                candidate = json.loads(json.dumps(data))
                candidate["hue"]["hue-monitor"].update(change)
                self.assertEqual(
                    studio.project_status(candidate, source_active=False)["sync"]["state"], "off"
                )
                self.assertEqual(
                    studio.project_status(candidate, source_active=None)["sync"]["state"], "unknown"
                )
        self.assertEqual(
            studio.project_status(data, source_active=False)["sync"]["state"], "foreign"
        )
        data["hue"]["hue-monitor"]["native_mode"] = "normal"
        data["hue"]["unselected"] = {
            "healthy": True,
            "native_age": 0.1,
            "native_on": True,
            "native_mode": "streaming",
        }
        self.assertEqual(studio.project_status(data, source_active=False)["sync"]["state"], "off")

    def test_source_cache_is_bounded_and_failure_returns_unknown(self):
        self.assertTrue(hasattr(studio, "SourceActivityCache"), "Source cache not implemented")
        now, calls = [0.0], []

        def fetch():
            calls.append(1)
            if len(calls) == 3:
                raise ValueError("PRIVATE_SOURCE_ERROR")
            return len(calls) == 2

        cache = studio.SourceActivityCache(fetch, clock=lambda: now[0])
        self.assertIs(cache.get(), False)
        self.assertIs(cache.get(), False)
        self.assertEqual(len(calls), 1)
        now[0] = 1.1
        self.assertIs(cache.get(), True)
        now[0] = 2.2
        self.assertIsNone(cache.get())

    def test_fresh_video_does_not_poll_source_control_and_idle_resolves_off(self):
        calls = []
        data = status()
        data["video"].update(active=True, ready=True, frame_age=0.1)
        data["preview"]["mode"] = "sync"
        with TemporaryDirectory(prefix="tmp_setup_live_source_") as directory:
            handler = studio.handler_for(
                Path(directory) / "missing-token",
                live_fetch=lambda: data,
                source_fetch=lambda: calls.append("source") or False,
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                url = f"http://127.0.0.1:{server.server_port}/api/live"
                with urllib.request.urlopen(url) as response:
                    self.assertEqual(json.load(response)["sync"]["state"], "active")
                self.assertEqual(calls, [])
                data["video"].update(active=False, ready=False)
                data["preview"]["mode"] = "scene"
                data["hue"]["hue-monitor"].update(
                    healthy=False, native_age=None, native_mode="streaming")
                with urllib.request.urlopen(url) as response:
                    self.assertEqual(json.load(response)["sync"]["state"], "off")
                self.assertEqual(calls, ["source"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

    def test_sleep_hold_returns_pause_without_any_network_or_fetch(self):
        calls = []
        with TemporaryDirectory(prefix="tmp_setup_live_hold_") as directory:
            root = Path(directory)
            state = root / "state"
            state.mkdir()
            (state / "live-output-hold").write_text("Owner sleeps")
            try:
                handler = studio.handler_for(
                    root / "credentials/missing",
                    state / "desk-layout.json",
                    live_fetch=lambda: calls.append("live"),
                    source_fetch=lambda: calls.append("source"),
                )
            except TypeError as error:
                self.fail("Read-only gateway injection missing: " + str(error))
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{server.server_port}/api/live"
                ) as r:
                    data = json.load(r)
                self.assertEqual(data["mode"], "paused")
                self.assertEqual(data["sync"]["state"], "paused")
                self.assertEqual(data["lamps"], {})
                self.assertEqual(calls, [])
            finally:
                server.shutdown()
                server.server_close()
                thread.join()


if __name__ == "__main__":
    unittest.main()
