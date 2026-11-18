"""Verified Sync wait reasons and the sanitized diagnostic export."""

import copy
import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "experiments" / "hue-bridge"))
import sync_health  # noqa: E402

PRIVATE = ("PRIVATE_DEVICE", "PRIVATE_RESOURCE", "192.168.", "TOKEN")


def event(state, reason, **metrics):
    return {"timestamp": "2026-11-17T00:00:00Z", "state": state, "reason": reason,
            "metrics": metrics, "elapsed": 1.0}


def status(**video):
    hue = {
        role: {"resource_id": "PRIVATE_RESOURCE-" + role, "healthy": True, "native_on": True,
               "native_mode": "normal", "native_age": 0.4, "native_brightness": 70,
               "last_result": "applied", "last_payload": {"key": "TOKEN"}, "writes": 3}
        for role in ("hue_left", "hue_center", "hue_right")
    }
    data = {
        "service": {"name": "lights-can-communicate-v2-adapter", "version": "0.1.0.dev9"},
        "targets": [
            {"side": side, "device": "PRIVATE_DEVICE-" + side, "target": "192.168.10.5" + side[0],
             "segment_count": 7, "status_age": 0.2,
             "native": {"identity_verified": True, "onOff": 1, "brightness": 40}}
            for side in ("left", "right")
        ],
        "commands_sent": 12,
        "rejected_sends": {"right": 1},
        "skipped_state": {},
        "hue_strip": {"resource_id": "PRIVATE_RESOURCE-strip", "healthy": True, "native_on": True,
                      "native_mode": "normal", "native_age": 0.3, "last_result": "applied"},
        "hue_play": hue,
        "power_owner": {"enforced": True, "roles": {
            "left": {"requested_on": True, "color_allowed": True, "revision": 2,
                     "grant_remaining": 1.5},
            "right": {"requested_on": True, "color_allowed": True, "revision": 4,
                      "grant_remaining": 1.5}}},
        "video": {
            "active": True, "frames_received": 1000, "frame_age": 0.05, "max_frame_gap": 0.2,
            "sequence": 77, "ready": True, "cleanup_pending": False, "native_owned": True,
            "frames_sent": 990, "last_result": "streaming",
            "last_safety_failure": {"phase": "stream", "native": [
                {"resource_id": "PRIVATE_RESOURCE-x", "healthy": True, "native_on": False,
                 "native_mode": "normal", "native_age": 0.1, "last_result": "applied"}]},
            "transitions": {"frames_received": 3, "frame_gap": 1},
            "diagnostics": {"events": [event("waiting", "starting"),
                                       event("streaming", "frames_received", frames_received=1)],
                            "events_written": 2, "write_errors": 0},
            "govee": {"targets": [{"device": "PRIVATE_DEVICE"}]},
        },
    }
    data["video"].update(video)
    return data


class DeriveTests(unittest.TestCase):
    def test_streaming_with_fresh_frames_and_no_blockers(self):
        health = sync_health.derive(status())
        self.assertEqual((health["state"], health["reason"]), ("streaming", "frames_received"))
        self.assertEqual(health["frames"], {"received": 1000, "age": 0.05, "fresh": True})
        self.assertEqual(health["output"], {"ready": True, "native_owned": True, "sent": 990,
                                            "cleanup_pending": False})
        self.assertEqual(health["blockers"], [])

    def test_missing_classification_stays_unknown_even_without_frames(self):
        data = status(frames_received=0, frame_age=None, last_result="waiting",
                      diagnostics={"events": []})
        health = sync_health.derive(data)
        self.assertEqual((health["state"], health["reason"]), ("unknown", "unknown"))
        self.assertFalse(health["frames"]["fresh"])
        self.assertEqual(sync_health.derive({})["reason"], "unknown")
        self.assertEqual(sync_health.derive(None)["state"], "unknown")

    def test_unrecognized_event_is_not_trusted(self):
        data = status(diagnostics={"events": [event("streaming", "guessed")]})
        self.assertEqual(sync_health.derive(data)["reason"], "unknown")

    def test_unrecognized_latest_event_does_not_revive_an_older_reason(self):
        # The latest event is the current state; an older reason may no longer hold.
        data = status(diagnostics={"events": [event("waiting", "native_unavailable"),
                                              event("streaming", "guessed")]})
        health = sync_health.derive(data)
        self.assertEqual((health["state"], health["reason"]), ("unknown", "unknown"))

    def test_power_owner_off_and_fence_are_blockers(self):
        data = status()
        data["power_owner"]["roles"]["right"].update(requested_on=False, color_allowed=False)
        data["power_owner"]["roles"]["left"]["color_allowed"] = False
        self.assertEqual(sync_health.derive(data)["blockers"], [
            {"role": "left", "code": "color_fenced"},
            {"role": "right", "code": "owner_off"},
        ])

    def test_native_off_requires_a_fresh_verified_reading(self):
        data = status()
        data["targets"][1]["native"]["onOff"] = 0
        data["hue_play"]["hue_center"]["native_on"] = False
        blockers = sync_health.derive(data)["blockers"]
        self.assertIn({"role": "right", "code": "native_off"}, blockers)
        self.assertIn({"role": "hue_center", "code": "native_off"}, blockers)
        stale = status()
        stale["targets"][1]["native"]["onOff"] = 0
        stale["targets"][1]["status_age"] = 3
        stale["hue_play"]["hue_center"].update(native_on=False, native_age=5)
        blockers = sync_health.derive(stale)["blockers"]
        self.assertIn({"role": "right", "code": "native_stale"}, blockers)
        self.assertIn({"role": "hue_center", "code": "native_stale"}, blockers)
        self.assertNotIn({"role": "right", "code": "native_off"}, blockers)
        unverified = status()
        unverified["targets"][0]["native"]["identity_verified"] = False
        self.assertIn({"role": "left", "code": "native_stale"},
                      sync_health.derive(unverified)["blockers"])

    def test_foreign_native_stream_only_when_not_owned(self):
        data = status(native_owned=False)
        data["hue_strip"]["native_mode"] = "streaming"
        self.assertIn({"role": "strip", "code": "native_foreign"},
                      sync_health.derive(data)["blockers"])
        owned = status()
        owned["hue_strip"]["native_mode"] = "streaming"
        self.assertEqual(sync_health.derive(owned)["blockers"], [])

    def test_only_selected_roles_are_reported(self):
        data = status()
        data["composition"] = {"enabled_roles": ["left", "hue_left"], "signature": "x"}
        data["power_owner"]["roles"]["right"]["requested_on"] = False
        data["hue_play"]["hue_right"]["native_on"] = False
        self.assertEqual(sync_health.derive(data)["blockers"], [])

    def test_wait_detail_is_the_adapter_classification_text(self):
        data = status(last_result="waiting for native on/normal", diagnostics={
            "events": [event("waiting", "native_unavailable")]})
        health = sync_health.derive(data)
        self.assertEqual((health["state"], health["reason"]), ("waiting", "native_unavailable"))
        self.assertEqual(health["detail"], "waiting for native on/normal")
        odd = status(last_result={"nested": "object"})
        self.assertIsNone(sync_health.derive(odd)["detail"])

    def test_derive_does_not_mutate_input(self):
        data = status()
        before = copy.deepcopy(data)
        sync_health.derive(data)
        sync_health.export(data)
        self.assertEqual(data, before)


class ExportTests(unittest.TestCase):
    def test_export_excludes_credentials_and_identifiers(self):
        text = json.dumps(sync_health.export(status()))
        for marker in PRIVATE:
            self.assertNotIn(marker, text)

    def test_export_correlates_source_forwarding_native_and_power(self):
        result = sync_health.export(status())
        self.assertEqual(result["version"], 1)
        self.assertEqual(result["service"], {"name": "lights-can-communicate-v2-adapter",
                                             "version": "0.1.0.dev9"})
        self.assertEqual(result["sync"]["reason"], "frames_received")
        self.assertEqual(result["video"]["frames_received"], 1000)
        self.assertEqual(result["video"]["frames_sent"], 990)
        self.assertEqual(result["video"]["transitions"], {"frames_received": 3, "frame_gap": 1})
        self.assertEqual([e["reason"] for e in result["video"]["events"]],
                         ["starting", "frames_received"])
        self.assertEqual(result["video"]["last_safety_failure"]["native"][0]["native_on"], False)
        self.assertEqual(result["power_owner"]["right"]["revision"], 4)
        self.assertEqual(result["govee"]["right"], {
            "on": True, "brightness": 40, "identity_verified": True, "status_age": 0.2,
            "segment_count": 7})
        self.assertEqual(result["native"]["strip"]["native_mode"], "normal")
        self.assertEqual(result["forwarding"], {"commands_sent": 12, "rejected_sends": {"right": 1},
                                                "skipped_state": {}})

    def test_export_is_bounded_and_drops_malformed_values(self):
        data = status(diagnostics={"events": [event("streaming", "frames_received")] * 500},
                      transitions={"x" * 500: 1, "frames_received": "many"})
        data["hue_play"] = {"PRIVATE_RESOURCE": {"healthy": True}}
        data["targets"].append({"side": "PRIVATE_DEVICE", "native": {}})
        result = sync_health.export(data)
        self.assertLessEqual(len(result["video"]["events"]), 64)
        self.assertEqual(result["video"]["transitions"], {})
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertLess(len(json.dumps(result)), 65536)

    def test_export_of_unusable_status(self):
        result = sync_health.export(None)
        self.assertEqual(result["sync"]["state"], "unknown")
        self.assertEqual(result["govee"], {})

    def test_export_includes_sanitized_power_changes(self):
        data = status()
        change = {"timestamp": "2026-11-18T20:00:00Z", "role": "right", "cause": "sync_hold",
                  "requested_on": True, "color_allowed": False, "sync_hold": True}
        data["power_owner"]["events"] = [
            change,
            {**change, "role": "PRIVATE_DEVICE"},
            {**change, "cause": "PRIVATE_CAUSE"},
            {**change, "timestamp": None},
            {**change, "requested_on": "PRIVATE", "token": "PRIVATE_TOKEN"},
        ] + [change] * 200
        events = sync_health.export(data)["power_events"]
        self.assertEqual(events[0], change)
        self.assertLessEqual(len(events), 64)
        self.assertNotIn("PRIVATE", json.dumps(sync_health.export(data)))
        self.assertEqual(sync_health.export(status())["power_events"], [])


class AdapterEndpointTests(unittest.TestCase):
    def test_diagnostics_route_is_authorized_and_sanitized(self):
        from live_adapter import PilotController, handler_for
        from power_owner import PowerOwner
        from test_live_adapter import LEFT, targets

        controller = PilotController(targets(), clock=lambda: 10)
        controller.update_statuses({LEFT: ({"onOff": 0, "identity_verified": True,
                                            "native_sample": 9.95}, 9.9)})
        gate = PowerOwner(targets())
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(
            controller, "adapter-secret", power_owner=gate, power_token="owner-secret"))
        worker = threading.Thread(target=server.serve_forever)
        worker.start()

        def get(path, header, key):
            request = urllib.request.Request(f"http://127.0.0.1:{server.server_port}" + path,
                                             headers={header: key})
            try:
                with urllib.request.urlopen(request, timeout=2) as response:
                    return response.status, response.read().decode()
            except urllib.error.HTTPError as error:
                with error:
                    return error.code, error.read().decode()

        try:
            self.assertEqual(get("/diagnostics", "X-Adapter-Token", "wrong")[0], 403)
            self.assertEqual(get("/diagnostics", "X-Power-Owner-Token", "owner-secret")[0], 403)
            code, body = get("/diagnostics", "X-Adapter-Token", "adapter-secret")
            self.assertEqual(code, 200)
            result = json.loads(body)
            self.assertEqual(result["version"], 1)
            self.assertEqual(result["govee"]["left"]["on"], False)
            self.assertIn({"role": "left", "code": "native_off"}, result["sync"]["blockers"])
            self.assertIn({"role": "left", "code": "owner_off"}, result["sync"]["blockers"])
            for target in targets():
                self.assertNotIn(target["device"], body)
                self.assertNotIn(target["ip"], body)
            self.assertNotIn("adapter-secret", body)
            self.assertNotIn("owner-secret", body)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(2)


if __name__ == "__main__":
    unittest.main()
