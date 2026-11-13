import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "experiments" / "hue-bridge"))
from entertainment_stream import NATIVE_AREA_ID  # noqa: E402
from hue_entertainment_relay import HueEntertainmentRelay  # noqa: E402
from hue_strip_proxy import PLAY_IDS, STRIP_ID  # noqa: E402


class FakeConnection:
    def __init__(self):
        self.frames = []
        self.closed = False

    def send(self, frame):
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
                {"channel_id": channel, "members": [{"service": {"rid": service}, "index": index}]}
                for channel, (service, index) in enumerate(
                    [
                        ("545ed014-5e70-4039-a5de-51a1daa1dc88", 0),
                        ("07aeef78-efdf-4c8b-b0f7-0202d20e6e0a", 0),
                        ("157ab718-3fd0-4ab2-a5e6-904c5defba6d", 0),
                        *[("967b2eb0-0eb2-4099-9807-cdfed3c6383c", i) for i in range(9)],
                    ]
                )
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
            self.config, requester=self.request, connection_factory=lambda config: self.connection
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
