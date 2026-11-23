import copy
import io
import sys
import types
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from lights_can_communicate_v2_core.native_profile import NativeHueProfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge"))
from hue_entertainment_relay import HueEntertainmentRelay  # noqa: E402
from test_native_profile import AREA as NATIVE_AREA_ID  # noqa: E402
from test_native_profile import PLAY_IDS, STRIP_ID, profile_data  # noqa: E402


class FakeConnection:
    def __init__(self):
        self.frames = []
        self.closed = False

    def send(self, frame, projected=None):
        self.frames.append(frame)

    def close(self):
        self.closed = True


class RelayTests(unittest.TestCase):
    def setUp(self):
        self.area = {
            "id": NATIVE_AREA_ID,
            "status": "inactive",
            "light_services": [
                {"rid": x, "rtype": "light"} for x in (STRIP_ID, *PLAY_IDS.values())
            ],
            "channels": [
                {"channel_id": item["channel_id"], "members": [{
                    "service": {"rid": item["member"]["rid"], "rtype": "entertainment"},
                    "index": item["member"]["index"],
                }]}
                for item in profile_data()["entertainment"]["channels"]
            ],
        }
        self.calls = []
        self.connection = FakeConnection()
        self.config = {
            "bridge": "192.168.10.21",
            "hue_key": "test-key",
            "client_key": "00" * 16,
            "area_id": NATIVE_AREA_ID,
            "application_id": "11111111-1111-1111-1111-111111111111",
        }
        self.relay = HueEntertainmentRelay(
            self.config, requester=self.request, connection_factory=lambda config: self.connection,
            profile=NativeHueProfile.from_dict(profile_data()),
        )

    def request(self, method, body=None):
        self.calls.append((method, copy.deepcopy(body)))
        if method == "GET":
            return copy.deepcopy(self.area)
        if body == {"action": "start"}:
            self.area.update(
                status="active", active_streamer={"rid": self.config["application_id"]}
            )
        elif body == {"action": "stop"}:
            self.area.update(status="inactive")
            self.area.pop("active_streamer", None)
        return {"errors": []}

    def test_start_and_stop_own_exact_area_without_power_fields(self):
        self.assertTrue(self.relay.start())
        self.relay.send({i: (65535, 0, 0) for i in range(14)})
        self.assertEqual(self.connection.frames[0][:9], b"HueStream")
        self.relay.stop()
        self.assertTrue(self.connection.closed)
        self.assertEqual(
            [b for m, b in self.calls if m == "PUT"], [{"action": "start"}, {"action": "stop"}]
        )

    def channels(self, frame):
        import struct
        return {row[0]: row[1:] for row in (struct.unpack("!BHHH", frame[i:i + 7])
                                            for i in range(52, len(frame), 7))}

    def test_an_override_replaces_only_its_lights_channels_until_released(self):
        self.assertTrue(self.relay.start())
        frame = {i: (1000, 2000, 3000) for i in range(14)}
        self.assertFalse(self.relay.streams("play-left"), "no frame sent yet")
        self.relay.send(frame)
        self.assertTrue(self.relay.streams("play-left"))
        self.relay.override("play-left", (65535, 65535, 65535))
        self.relay.send(frame)
        shown = self.channels(self.connection.frames[-1])
        self.assertEqual(shown[1], (65535, 65535, 65535))
        self.assertNotEqual(shown[0], (65535, 65535, 65535))
        self.assertTrue(all(shown[c] != (65535, 65535, 65535) for c in range(2, 12)))
        self.relay.override("hue-monitor", (0, 0, 0))
        self.relay.send(frame)
        shown = self.channels(self.connection.frames[-1])
        self.assertTrue(all(shown[c] == (0, 0, 0) for c in range(3, 12)))
        self.relay.override("play-left", None)
        self.relay.override("hue-monitor", None)
        self.relay.send(frame)
        self.assertNotEqual(self.channels(self.connection.frames[-1])[1], (65535, 65535, 65535))
        self.relay.override("hue-monitor", (0, 0, 0))
        self.relay.stop()
        self.assertEqual(self.relay.overrides, {}, "stop ends every override")
        self.assertFalse(self.relay.streams("hue-monitor"))
        # Govee sides and old role names are not native lights.
        for light in ("left", "strip", "hue_left"):
            with self.subTest(light=light), self.assertRaises(ValueError):
                self.relay.override(light, (0, 0, 0))

    def test_foreign_stream_is_never_taken_over_or_stopped(self):
        self.area.update(status="active", active_streamer={"rid": "foreign"})
        self.assertFalse(self.relay.start())
        self.relay.stop()
        self.assertFalse(any(m == "PUT" for m, _ in self.calls))

    def test_unselected_member_or_wrong_area_blocks_start(self):
        self.area["light_services"].append({"rid": "unrelated", "rtype": "light"})
        with self.assertRaises(ValueError):
            self.relay.start()
        with self.assertRaises(ValueError):
            HueEntertainmentRelay({**self.config, "area_id": "other"})
        self.assertFalse(any(m == "PUT" for m, _ in self.calls))

    def test_native_channel_reassignment_blocks_start(self):
        self.area["channels"][0]["members"][0]["index"] = 8
        with self.assertRaises(ValueError):
            self.relay.start()
        self.assertFalse(any(m == "PUT" for m, _ in self.calls))

    def test_losing_stream_ownership_prevents_cleanup_of_replacement(self):
        self.relay.start()
        self.area["active_streamer"] = {"rid": "foreign"}
        self.relay.stop()
        self.assertNotIn(("PUT", {"action": "stop"}), self.calls)

    def test_losing_stream_ownership_blocks_next_frame(self):
        self.relay.start()
        self.area["active_streamer"] = {"rid": "foreign"}
        with self.assertRaises(ValueError):
            self.relay.send({i: (0, 0, 0) for i in range(14)})
        self.assertEqual(self.connection.frames, [])

    def test_failed_stop_retains_ownership_for_cleanup_retry(self):
        self.relay.start()
        original = self.relay.requester
        self.relay.requester = lambda *args: (_ for _ in ()).throw(OSError("temporary outage"))
        with self.assertRaises(OSError):
            self.relay.stop()
        self.assertTrue(self.relay.owned)
        self.relay.requester = original
        self.relay.stop()
        self.assertFalse(self.relay.owned)
        self.assertEqual(self.area["status"], "inactive")

    def test_start_response_timeout_releases_possibly_started_owned_stream(self):
        original = self.relay.requester

        def request(method, body=None):
            result = original(method, body)
            if body == {"action": "start"}:
                raise OSError("response lost")
            return result

        self.relay.requester = request
        with self.assertRaises(OSError):
            self.relay.start()
        self.assertEqual(self.area["status"], "inactive")

    def test_recovery_releases_only_our_stale_stream(self):
        self.area.update(status="active", active_streamer={"rid": self.config["application_id"]})
        self.relay.recover()
        self.assertEqual(self.area["status"], "inactive")

    def test_recovery_read_failure_preserves_possible_ownership_for_retry(self):
        self.area.update(status="active", active_streamer={"rid": self.config["application_id"]})
        original = self.relay.requester
        self.relay.requester = lambda *args: (_ for _ in ()).throw(OSError("temporary outage"))
        with self.assertRaises(OSError):
            self.relay.recover()
        self.relay.requester = original
        self.relay.stop()
        self.assertEqual(self.area["status"], "inactive")

    def test_unknown_native_status_never_authorizes_stream_start(self):
        for status in (None, "unknown", "active-but-unconfirmed"):
            with self.subTest(status=status):
                self.area["status"] = status
                with self.assertRaises(ValueError):
                    self.relay.start()
                self.assertFalse(any(method == "PUT" for method, _ in self.calls))

    def test_malformed_native_area_is_rejected_without_start_command(self):
        valid = copy.deepcopy(self.area)
        for response in (None, [], "invalid",
                         {**valid, "light_services": [None]},
                         {**valid, "channels": [None] * 12}):
            with self.subTest(response=response):
                self.relay.requester = lambda *_: response
                with self.assertRaises(ValueError):
                    self.relay.start()
                self.assertFalse(self.relay.owned)

    def test_malformed_http_json_is_reported_as_recoverable_native_error(self):
        for payload in (b"null", b"[]", b'{"data":null}', b'{"data":[null]}'):
            with self.subTest(payload=payload):
                response = io.BytesIO(payload)
                opener = types.SimpleNamespace(open=lambda *_args, **_kwargs: response)
                with patch("hue_entertainment_relay.urllib.request.build_opener",
                           return_value=opener), self.assertRaises(ValueError):
                    self.relay.request("GET")
                self.assertTrue(response.closed)

    def test_native_http_error_response_is_closed_for_recovery_retry(self):
        response = io.BytesIO(b"temporarily unavailable")
        error = urllib.error.HTTPError("https://bridge/area", 503, "Unavailable", {}, response)
        opener = types.SimpleNamespace(open=lambda *_args, **_kwargs: (_ for _ in ()).throw(error))
        with patch("hue_entertainment_relay.urllib.request.build_opener", return_value=opener):
            with self.assertRaises(OSError):
                self.relay.request("GET")
        self.assertTrue(response.closed)

    def test_unknown_status_during_cleanup_retains_owned_fence_until_read_recovers(self):
        self.relay.start()
        self.area["status"] = "unknown"
        with self.assertRaises(ValueError):
            self.relay.stop()
        self.assertTrue(self.relay.owned)
        self.area["status"] = "active"
        self.relay.stop()
        self.assertFalse(self.relay.owned)
        self.assertEqual(self.area["status"], "inactive")

    def test_failed_dtls_handshake_releases_owned_area(self):
        def unavailable(_):
            raise OSError("handshake failed")

        self.relay.connection_factory = unavailable
        with self.assertRaises(OSError):
            self.relay.start()
        self.assertFalse(self.relay.owned)
        self.assertEqual(self.area["status"], "inactive")

    def test_lost_stop_response_is_confirmed_on_next_cleanup(self):
        self.relay.start()
        original = self.relay.requester

        def lost_response(method, body=None):
            result = original(method, body)
            if body == {"action": "stop"}:
                raise OSError("response lost after applying stop")
            return result

        self.relay.requester = lost_response
        with self.assertRaises(OSError):
            self.relay.stop()
        self.assertTrue(self.relay.owned)
        self.assertTrue(self.connection.closed)
        self.relay.requester = original
        self.relay.stop()
        self.assertFalse(self.relay.owned)
        self.assertEqual(self.area["status"], "inactive")

    def test_missing_active_owner_keeps_cleanup_fenced(self):
        self.relay.start()
        self.area["active_streamer"] = None
        with self.assertRaises(ValueError):
            self.relay.stop()
        self.assertTrue(self.relay.owned)
        self.area["active_streamer"] = {"rid": self.config["application_id"]}
        self.relay.stop()
        self.assertFalse(self.relay.owned)

    def test_stop_ack_without_state_change_requires_cleanup_retry(self):
        self.relay.start()
        original = self.relay.requester

        def ignored_stop(method, body=None):
            if body == {"action": "stop"}:
                return {"errors": []}
            return original(method, body)

        self.relay.requester = ignored_stop
        with self.assertRaises(OSError):
            self.relay.stop()
        self.assertTrue(self.relay.owned)
        self.relay.requester = original
        self.relay.stop()
        self.assertFalse(self.relay.owned)

    def test_start_ack_remaining_inactive_with_null_owner_is_recoverable(self):
        original = self.relay.requester

        def ignored_start(method, body=None):
            if body == {"action": "start"}:
                self.area["active_streamer"] = None
                return {"errors": []}
            return original(method, body)

        self.relay.requester = ignored_start
        with self.assertRaises(ValueError):
            self.relay.start()
        self.assertFalse(self.relay.owned)
