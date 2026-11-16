"""Discovery metadata never enrolls a device or bypasses the sole LAN owner."""

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "experiments/hue-bridge"))
from desk_layout import positions, validate_layout  # noqa: E402
from live_adapter import PilotController, handler_for  # noqa: E402
from pilot_transport import PilotTransport  # noqa: E402
from test_desk_layout import layout  # noqa: E402
from test_live_adapter import LEFT, targets  # noqa: E402

import smart_lighting as lan  # noqa: E402


class DiscoveryIntegrationTests(unittest.TestCase):
    def test_real_catalog_placement_roundtrips_without_changing_controlled_roles(self):
        value = layout()
        existing_positions = positions(value)
        added = {
            "id": "device-" + "a" * 32,
            "name": "Discovered floor light",
            "model": "H6076",
            "shape": "floor",
            "zones": 7,
            "scale": 65,
            "orientation": "vertical",
            "positionCm": {"x": -100, "height": 0},
        }
        value["devices"].append(added)
        try:
            saved = validate_layout(value)
        except ValueError as error:
            self.fail("Valid identity-bound placement was rejected: " + str(error))
        self.assertEqual(saved["devices"][-1], added)
        self.assertEqual(positions(saved), existing_positions)
        for change in [
            {"id": "device-injected"},
            {"zones": True},
            {"zones": 0},
            {"model": ""},
            {"shape": "socket"},
            {"name": "x" * 81},
        ]:
            invalid = {**value, "devices": value["devices"][:-1] + [{**added, **change}]}
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_layout(invalid)

    def test_catalog_http_is_authenticated_and_does_not_submit_device_commands(self):
        class Catalog:
            def snapshot(self):
                return {
                    "connected": True,
                    "devices": [
                        {"id": "device-" + "a" * 32, "name": "Unselected", "supported": True}
                    ],
                    "providers": {},
                }

        sent = []
        controller = PilotController(targets(), sender=lambda *args: sent.append(args))
        try:
            handler = handler_for(controller, "private-adapter-key", catalog=Catalog())
        except TypeError as error:
            self.fail("Authenticated discovery endpoint is missing: " + str(error))
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        url = f"http://127.0.0.1:{server.server_port}/discovery"
        try:
            with self.assertRaises(urllib.error.HTTPError) as rejected:
                opener.open(url)
            self.assertEqual(rejected.exception.code, 403)
            rejected.exception.close()
            request = urllib.request.Request(
                url, headers={"X-Adapter-Token": "private-adapter-key"}
            )
            with opener.open(request) as response:
                result = json.load(response)
            self.assertEqual(result["devices"][0]["name"], "Unselected")
            self.assertNotIn("private-adapter-key", json.dumps(result))
            self.assertEqual(sent, [])
            self.assertEqual(len(controller.targets), 2)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_single_listener_observes_unselected_scan_without_enrolling_or_sending(self):
        observations = []

        class Catalog:
            def observe_govee(self, data, peer_ip, now=None):
                observations.append((data["device"], peer_ip))

        try:
            transport = PilotTransport(targets(), lambda _: None, discovery=Catalog())
        except TypeError as error:
            self.fail("The existing sole listener has no discovery observer: " + str(error))
        with patch.object(lan, "govee_send", side_effect=AssertionError("Discovery sent control")):
            transport.handle(
                {
                    "msg": {
                        "cmd": "scan",
                        "data": {
                            "device": "CC:CC:CC:CC:CC:CC:CC:CC",
                            "sku": "H6076",
                        },
                    }
                },
                "192.168.99.52",
                100,
            )
        self.assertEqual(observations, [("CC:CC:CC:CC:CC:CC:CC:CC", "192.168.99.52")])
        self.assertNotIn("CC:CC:CC:CC:CC:CC:CC:CC", transport.ids)
        self.assertEqual(len(transport.targets), 2)

    def test_transport_failure_marks_discovery_unavailable_without_new_targets(self):
        states, published = [], []

        class Catalog:
            def provider_state(self, provider, state):
                states.append((provider, state))

        class Stop:
            stopped = False

            def is_set(self):
                return self.stopped

            def wait(self, timeout):
                self.stopped = True

        transport = PilotTransport(targets(), published.append, discovery=Catalog())
        with patch.object(transport, "receive_loop", side_effect=OSError("Socket unavailable")):
            transport.run(Stop())
        self.assertEqual(states, [("govee", "unavailable")])
        self.assertEqual(len(published[-1]), 2)
        self.assertTrue(all(value[0]["onOff"] == 0 for value in published[-1].values()))

    def test_catalog_failure_cannot_break_selected_identity_and_power_observation(self):
        class BrokenCatalog:
            def observe_govee(self, *args, **kwargs):
                raise RuntimeError("Metadata-only failure")

        published = []
        try:
            transport = PilotTransport(targets(), published.append, discovery=BrokenCatalog())
        except TypeError as error:
            self.fail("Discovery observer isolation is missing: " + str(error))
        transport.handle({"msg": {"cmd": "devStatus", "data": {"onOff": 1}}}, "192.168.10.50", 100)
        transport.handle(
            {"msg": {"cmd": "scan", "data": {"device": LEFT, "sku": "H6076"}}}, "192.168.10.50", 100
        )
        self.assertTrue(published[-1][LEFT][0]["identity_verified"])
        self.assertEqual(published[-1][LEFT][0]["onOff"], 1)
        transport.handle(
            {"msg": {"cmd": "devStatus", "data": {"onOff": 0}}}, "192.168.10.50", 100.1
        )
        self.assertEqual(published[-1][LEFT][0]["onOff"], 0)


if __name__ == "__main__":
    unittest.main()
