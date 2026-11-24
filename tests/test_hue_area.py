"""A light's membership in the paired native Entertainment area (#7 stage 5b)."""

import copy
import hashlib
import io
import json
import ssl
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge"))
import hue_area  # noqa: E402
from hue_area import (  # noqa: E402
    AREA_NAME,
    AreaBusy,
    AreaMissing,
    AreaShared,
    CertificateChanged,
    HueArea,
)

AREA = "e5555555-5555-4555-8555-555555555555"
LIGHT = "a0000000-0000-4000-8000-00000000000a"
SERVICE = "b0000000-0000-4000-8000-00000000000b"
OTHER_LIGHT = "c0000000-0000-4000-8000-00000000000c"
OTHER_SERVICE = "d0000000-0000-4000-8000-00000000000d"
CONFIG = {"bridge": "192.168.10.20", "hue_key": "k" * 40}


def channel(channel_id, service, index):
    return {"channel_id": channel_id,
            "members": [{"service": {"rid": service, "rtype": "entertainment"}, "index": index}]}


def area(lights=(LIGHT, OTHER_LIGHT), channels=None):
    channels = channels if channels is not None else [
        channel(0, OTHER_SERVICE, 0), channel(1, SERVICE, 1), channel(2, SERVICE, 0),
        channel(3, SERVICE, 2)]
    return {"errors": [], "data": [{
        "id": AREA, "type": "entertainment_configuration", "status": "inactive",
        "light_services": [{"rid": light, "rtype": "light"} for light in lights],
        "channels": channels}]}


SERVICES = {"errors": [], "data": [
    {"id": OTHER_SERVICE, "renderer_reference": {"rid": OTHER_LIGHT, "rtype": "light"}},
    {"id": SERVICE, "renderer_reference": {"rid": LIGHT, "rtype": "light"}},
    {"id": "f0000000-0000-4000-8000-00000000000f", "renderer": False},
]}


def reader(area_response=None, services=SERVICES):
    responses = {
        "/clip/v2/resource/entertainment_configuration/" + AREA: area_response or area(),
        "/clip/v2/resource/entertainment": services,
    }
    calls = []

    def requester(path, method="GET", body=None):
        calls.append(path)
        return responses[path]

    return HueArea(CONFIG, AREA, requester=requester), calls


class Bridge:
    """A bridge that rebuilds the area's lights and channels from written locations."""

    SEGMENTS = {SERVICE: 3, OTHER_SERVICE: 1}
    RENDERERS = {SERVICE: LIGHT, OTHER_SERVICE: OTHER_LIGHT}

    def __init__(self, services=(OTHER_SERVICE,), status="inactive", shared=False):
        # shared: the bridge folds the new light into the other light's channel, as it
        # does for a second gradient light.
        self.status, self.writes, self.shared = status, [], shared
        self.rows = [{"service": {"rid": service, "rtype": "entertainment"},
                      "positions": [{"x": -0.5, "y": 0.8, "z": 0}],
                      "equalization_factor": 0.7} for service in services]

    def area(self):
        channels = []
        for row in self.rows:
            service = row["service"]["rid"]
            if self.shared and service == SERVICE and channels:
                channels[0]["members"].append({"service": {"rid": service,
                                                           "rtype": "entertainment"},
                                               "index": 0})
                continue
            for index in range(self.SEGMENTS[service]):
                channels.append({**channel(len(channels), service, index),
                                 "position": dict(row["positions"][0])})
        return {"errors": [], "data": [{
            "id": AREA, "type": "entertainment_configuration", "status": self.status,
            "light_services": [{"rid": self.RENDERERS[row["service"]["rid"]], "rtype": "light"}
                               for row in self.rows],
            "channels": channels, "locations": {"service_locations": copy.deepcopy(self.rows)}}]}

    def __call__(self, path, method="GET", body=None):
        if path == "/clip/v2/resource/entertainment":
            return SERVICES
        if method == "PUT":
            self.writes.append(copy.deepcopy(body))
            self.rows = copy.deepcopy(body["locations"]["service_locations"])
            return {"errors": [], "data": [{"rid": AREA, "rtype": "entertainment_configuration"}]}
        return self.area()


class HueAreaReaderTests(unittest.TestCase):
    def test_a_member_keeps_its_place_and_reports_its_service_and_segments(self):
        hue, calls = reader()
        self.assertEqual(hue.join(LIGHT), ({"service": SERVICE, "segments": 3}, False))
        self.assertEqual(hue.join(OTHER_LIGHT), ({"service": OTHER_SERVICE, "segments": 1}, False))
        self.assertEqual(calls[0], "/clip/v2/resource/entertainment_configuration/" + AREA)

    def test_resource_ids_are_compared_and_returned_in_canonical_form(self):
        upper = {"errors": [], "data": [
            {"id": SERVICE.upper(), "renderer_reference": {"rid": LIGHT.upper(),
                                                           "rtype": "light"}}]}
        hue, _ = reader(area(channels=[channel(0, SERVICE.upper(), 0)]), upper)
        self.assertEqual(hue.join(LIGHT.upper()), ({"service": SERVICE, "segments": 1}, False))

    def test_unreadable_answers_raise(self):
        broken = [
            (area(channels=[channel(0, SERVICE, 1)]), SERVICES),  # index gap
            (area(channels=[channel(0, SERVICE, 0), channel(1, SERVICE, 0)]), SERVICES),
            (area(channels=[channel(0, OTHER_SERVICE, 0)]), SERVICES),  # no channel
            (area(), {"errors": [], "data": []}),  # no renderer service
            (area(), {"errors": [{"description": "x"}], "data": []}),
            ({"errors": [], "data": []}, SERVICES),
            ({"errors": [], "data": [{"id": OTHER_SERVICE}]}, SERVICES),
            (area(channels="x"), SERVICES),
        ]
        for area_response, services in broken:
            with self.subTest(area=area_response, services=services):
                hue, _ = reader(area_response, services)
                with self.assertRaises(ValueError):
                    hue.join(LIGHT)

    def test_configuration_is_checked_without_io(self):
        for config, area_id in (({}, AREA), ({"bridge": "192.168.10.20"}, AREA),
                                (CONFIG, "not-an-area"), (CONFIG, "")):
            with self.subTest(config=config, area_id=area_id), self.assertRaises(ValueError):
                HueArea(config, area_id, requester=lambda *call: self.fail(call))


NEW_AREA = "f1111111-1111-4111-8111-111111111111"


class Creating:
    """A bridge where the core creates its own area; ``lost`` names an area that
    the bridge no longer has."""

    def __init__(self, shared=False, lost=None, contains=True):
        self.areas, self.calls, self.shared, self.lost = {}, [], shared, lost
        self.contains = contains

    def __call__(self, path, method="GET", body=None):
        self.calls.append((method, path, copy.deepcopy(body)))
        if path == "/clip/v2/resource/entertainment":
            return SERVICES
        base = "/clip/v2/resource/entertainment_configuration"
        if method == "POST" and path == base:
            self.areas[NEW_AREA] = body
            return {"errors": [], "data": [{"rid": NEW_AREA,
                                            "rtype": "entertainment_configuration"}]}
        rid = path.rsplit("/", 1)[1]
        if method == "DELETE":
            self.areas.pop(rid, None)
            return {"errors": [], "data": [{"rid": rid}]}
        if rid not in self.areas:
            raise AreaMissing("Hue area not found")
        members = [SERVICE] if self.contains else []
        channels = [channel(0, SERVICE, 0), channel(1, SERVICE, 1), channel(2, SERVICE, 2)]
        if self.shared:
            channels[0]["members"].append(channels.pop(1)["members"][0])
        return {"errors": [], "data": [{
            "id": rid, "type": "entertainment_configuration", "status": "inactive",
            "light_services": [{"rid": LIGHT, "rtype": "light"} for _ in members],
            "channels": channels if members else []}]}


class HueAreaCreationTests(unittest.TestCase):
    def test_the_first_hue_light_creates_the_cores_own_area(self):
        bridge = Creating()
        hue = HueArea(CONFIG, None, requester=bridge)
        self.assertIsNone(hue.area_id)
        self.assertEqual(hue.join(LIGHT), ({"service": SERVICE, "segments": 3}, True))
        self.assertEqual(hue.area_id, NEW_AREA)
        self.assertEqual(bridge.areas[NEW_AREA], {
            "type": "entertainment_configuration", "metadata": {"name": AREA_NAME},
            "configuration_type": "screen",
            "locations": {"service_locations": [
                {"service": {"rid": SERVICE, "rtype": "entertainment"},
                 "positions": [{"x": 0, "y": 0, "z": 0}]}]}})
        self.assertEqual(AREA_NAME, "Light Studio")
        # Without an area there is nothing to leave or restore.
        self.assertIsNone(HueArea(CONFIG, None, requester=bridge).leave(LIGHT))

    def test_a_failed_creation_deletes_the_new_area(self):
        for bridge, error in ((Creating(shared=True), AreaShared),
                              (Creating(contains=False), ValueError)):
            hue = HueArea(CONFIG, None, requester=bridge)
            with self.subTest(error=error), self.assertRaises(error):
                hue.join(LIGHT)
            self.assertEqual(bridge.areas, {})
            self.assertIsNone(hue.area_id)

    def test_abandon_deletes_an_area_created_for_a_failed_enrollment(self):
        bridge = Creating()
        hue = HueArea(CONFIG, None, requester=bridge)
        hue.join(LIGHT)
        hue.abandon(LIGHT)
        self.assertEqual(bridge.areas, {})
        self.assertIsNone(hue.area_id)

    def test_abandon_only_takes_the_light_out_of_an_existing_area(self):
        bridge = Bridge()
        hue = HueArea(CONFIG, AREA, requester=bridge)
        hue.join(LIGHT)
        hue.abandon(LIGHT)
        self.assertEqual(bridge.rows, Bridge().rows)
        self.assertEqual(hue.area_id, AREA)

    def test_a_lost_area_is_replaced_only_without_dependent_hue_lights(self):
        bridge = Creating()
        hue = HueArea(CONFIG, AREA, requester=bridge)
        with self.assertRaises(AreaMissing):
            hue.join(LIGHT)
        self.assertEqual(hue.area_id, AREA)
        self.assertFalse([call for call in bridge.calls if call[0] == "POST"])
        self.assertEqual(hue.join(LIGHT, replace_missing=True)[1], True)
        self.assertEqual(hue.area_id, NEW_AREA)

    def test_an_uncertain_creation_removes_only_the_area_it_may_have_left(self):
        base = "/clip/v2/resource/entertainment_configuration"
        left = {"id": NEW_AREA, "metadata": {"name": AREA_NAME}, "locations": {
            "service_locations": [{"service": {"rid": SERVICE, "rtype": "entertainment"}}]}}
        owners = [
            {"id": AREA, "metadata": {"name": "Mykola Office"}, "locations": {
                "service_locations": [{"service": {"rid": SERVICE, "rtype": "entertainment"}}]}},
            {"id": "f2222222-2222-4222-8222-222222222222", "metadata": {"name": AREA_NAME},
             "locations": {"service_locations": [
                 {"service": {"rid": OTHER_SERVICE, "rtype": "entertainment"}}]}}]
        deleted = []

        def bridge(path, method="GET", body=None):
            if path == "/clip/v2/resource/entertainment":
                return SERVICES
            if method == "POST":
                raise OSError("timed out")
            if method == "DELETE":
                deleted.append(path.rsplit("/", 1)[1])
                return {"errors": [], "data": []}
            self.assertEqual(path, base)
            return {"errors": [], "data": [left, *owners]}

        with self.assertRaises(OSError):
            HueArea(CONFIG, None, requester=bridge).join(LIGHT)
        self.assertEqual(deleted, [NEW_AREA])

    def test_a_created_area_answers_201(self):
        class Created(FakeConnection):
            def getresponse(self):
                body = {"errors": [], "data": [{"rid": NEW_AREA}]}
                answer = io.BytesIO(json.dumps(body).encode())
                return mock.Mock(status=201, read1=answer.read1)

        with tempfile.TemporaryDirectory(prefix="tmp_pin_") as folder:
            hue = HueArea(CONFIG, None, pin_file=Path(folder) / "hue-bridge-id")
            with mock.patch.object(hue_area.http.client, "HTTPSConnection", Created):
                self.assertEqual(hue._data("/clip/v2/resource/entertainment_configuration",
                                           "POST", {}), [{"rid": NEW_AREA}])

    def test_a_missing_area_is_reported_only_for_its_own_path(self):
        class Missing(FakeConnection):
            def getresponse(self):
                return mock.Mock(status=404, read1=io.BytesIO(b"").read1)

        with tempfile.TemporaryDirectory(prefix="tmp_pin_") as folder:
            hue = HueArea(CONFIG, AREA, pin_file=Path(folder) / "hue-bridge-id")
            with mock.patch.object(hue_area.http.client, "HTTPSConnection", Missing):
                with self.assertRaises(AreaMissing):
                    hue._request(hue._path)
                with self.assertRaises(OSError) as other:
                    hue._request("/clip/v2/resource/entertainment")
                self.assertNotIsInstance(other.exception, AreaMissing)
                with self.assertRaises(OSError) as written:
                    hue._request(hue._path, "PUT", {})
                self.assertNotIsInstance(written.exception, AreaMissing)


class HueAreaChangeTests(unittest.TestCase):
    def test_join_appends_the_light_at_the_center_and_keeps_the_other_rows(self):
        bridge = Bridge()
        hue = HueArea(CONFIG, AREA, requester=bridge)
        self.assertEqual(hue.join(LIGHT), ({"service": SERVICE, "segments": 3}, True))
        rows = bridge.writes[0]["locations"]["service_locations"]
        self.assertEqual(rows[0], Bridge().rows[0])
        # The new light takes the free point farthest from the other channels.
        self.assertEqual(rows[1], {"service": {"rid": SERVICE, "rtype": "entertainment"},
                                   "positions": [{"x": 1, "y": -1, "z": -1}]})
        # A member is kept as it is.
        self.assertEqual(hue.join(LIGHT), ({"service": SERVICE, "segments": 3}, False))
        self.assertEqual(len(bridge.writes), 1)

    def test_shared_channels_are_refused_and_the_area_is_restored(self):
        bridge = Bridge(shared=True)
        with self.assertRaises(AreaShared):
            HueArea(CONFIG, AREA, requester=bridge).join(LIGHT)
        self.assertEqual(bridge.writes[-1], {"locations": {"service_locations": Bridge().rows}})
        self.assertEqual(bridge.rows, Bridge().rows)
        # A member that already shares channels is refused as well, and kept.
        shared = Bridge((OTHER_SERVICE, SERVICE), shared=True)
        with self.assertRaises(AreaShared):
            HueArea(CONFIG, AREA, requester=shared).join(LIGHT)
        self.assertEqual(shared.writes, [])

    def test_an_uncertain_write_restores_the_area(self):
        class Lost(Bridge):
            # The bridge applies the first write but its answer is lost.
            def __call__(self, path, method="GET", body=None):
                result = super().__call__(path, method, body)
                if method == "PUT" and len(self.writes) == 1:
                    raise OSError("answer lost")
                return result

        for start, call in (((OTHER_SERVICE,), "join"), ((OTHER_SERVICE, SERVICE), "leave")):
            with self.subTest(call=call):
                bridge = Lost(start)
                rows = copy.deepcopy(bridge.rows)
                with self.assertRaises(OSError):
                    getattr(HueArea(CONFIG, AREA, requester=bridge), call)(LIGHT)
                self.assertEqual(bridge.rows, rows)
                self.assertEqual(bridge.writes[-1], {"locations": {"service_locations": rows}})

    def test_leave_removes_only_the_light(self):
        bridge = Bridge((OTHER_SERVICE, SERVICE))
        hue = HueArea(CONFIG, AREA, requester=bridge)
        rows = copy.deepcopy(bridge.rows)
        self.assertEqual(hue.leave(LIGHT), rows)
        self.assertEqual(bridge.writes, [{"locations": {"service_locations": Bridge().rows}}])
        self.assertIsNone(hue.leave(LIGHT))
        self.assertEqual(len(bridge.writes), 1)
        # A refused removal puts the light back where it was.
        hue.rejoin(rows)
        self.assertEqual(bridge.rows, rows)

    def test_a_streaming_area_is_not_changed(self):
        for status in ("active", None):
            with self.subTest(status=status):
                bridge = Bridge((OTHER_SERVICE, SERVICE), status=status)
                hue = HueArea(CONFIG, AREA, requester=bridge)
                with self.assertRaises(AreaBusy):
                    hue.join(LIGHT)
                with self.assertRaises(AreaBusy):
                    hue.leave(LIGHT)
                self.assertEqual(bridge.writes, [])

    def test_a_write_that_the_bridge_ignores_or_rejects_raises(self):
        class Ignoring(Bridge):
            def __call__(self, path, method="GET", body=None):
                if method == "PUT":
                    self.writes.append(body)
                    return {"errors": [], "data": []}
                return super().__call__(path, method, body)

        class Rejecting(Bridge):
            def __call__(self, path, method="GET", body=None):
                if method == "PUT":
                    return {"errors": [{"description": "too many channels"}], "data": []}
                return super().__call__(path, method, body)

        with self.assertRaises(ValueError):
            HueArea(CONFIG, AREA, requester=Ignoring()).join(LIGHT)
        with self.assertRaises(ValueError):
            HueArea(CONFIG, AREA, requester=Ignoring((OTHER_SERVICE, SERVICE))).leave(LIGHT)
        with self.assertRaises(ValueError):
            HueArea(CONFIG, AREA, requester=Rejecting()).join(LIGHT)
        with self.assertRaises(ValueError):
            HueArea(CONFIG, AREA, requester=Bridge()).join("not-a-uuid")


class FakeConnection:
    """An HTTPS connection to a bridge that presents ``certificate``."""

    certificate = {"subject": ((("countryName", "NL"),), (("organizationName", "Philips Hue"),),
                               (("commonName", "ECB5FAFFFEA26D51"),))}
    instances = []
    ORIGINAL = None

    def __init__(self, host, port, timeout, context):
        self.context = context
        self.host, self.requests, self.closed = host, [], False
        FakeConnection.instances.append(self)

    def connect(self):
        self.sock = mock.Mock(getpeercert=lambda: self.certificate)

    def request(self, method, path, body=None, headers=None):
        self.requests.append((method, path, headers))

    def getresponse(self):
        answer = io.BytesIO(json.dumps({"errors": [], "data": []}).encode())
        return mock.Mock(status=200, read1=answer.read1)

    def close(self):
        self.closed = True


FakeConnection.ORIGINAL = copy.deepcopy(FakeConnection.certificate)


def certificate_for(name):
    return {"subject": ((("commonName", name),),)}


class PinnedTransportTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="tmp_hue_pin_")
        self.addCleanup(temp.cleanup)
        self.pin = Path(temp.name) / "state" / "hue-bridge-certificate.sha256"
        FakeConnection.instances = []
        FakeConnection.certificate = copy.deepcopy(FakeConnection.ORIGINAL)
        patcher = mock.patch.object(hue_area.http.client, "HTTPSConnection", FakeConnection)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.hue = HueArea(CONFIG, AREA, pin_file=self.pin)

    def test_the_signify_root_ca_is_required_and_the_first_bridge_is_pinned(self):
        self.hue._request("/clip/v2/resource/entertainment")
        context = FakeConnection.instances[-1].context
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertEqual(len(context.get_ca_certs()), 1)
        self.assertEqual(self.pin.read_text().strip(), "ecb5fafffea26d51")
        self.assertEqual(stat.S_IMODE(self.pin.stat().st_mode), 0o600)
        self.hue._request("/clip/v2/resource/entertainment")
        sent = FakeConnection.instances[-1].requests
        self.assertEqual(sent[0][2]["hue-application-key"], CONFIG["hue_key"])
        self.assertTrue(all(connection.closed for connection in FakeConnection.instances))

    def test_another_bridge_never_receives_the_application_key(self):
        self.hue._request("/clip/v2/resource/entertainment")
        for certificate in (certificate_for("001788fffe000000"), certificate_for("bridge"),
                            {"subject": ()}, None):
            with self.subTest(certificate=certificate):
                FakeConnection.certificate = certificate
                with self.assertRaises(OSError):
                    self.hue._request("/clip/v2/resource/entertainment")
                self.assertEqual(FakeConnection.instances[-1].requests, [])
                self.assertTrue(FakeConnection.instances[-1].closed)
        self.assertIsInstance(CertificateChanged(), OSError)

    def test_the_embedded_root_is_the_signify_hue_bridge_ca(self):
        der = ssl.PEM_cert_to_DER_cert(hue_area.SIGNIFY_ROOT_CA)
        self.assertEqual(hashlib.sha256(der).hexdigest().upper(),
                         "F0BD8E6509E82F774D63BC009D5388C969FE3DCF7D6D541D6351B72B898D8ACF")

    def test_the_transport_requires_a_pin_file(self):
        with self.assertRaises(ValueError):
            HueArea(CONFIG, AREA)


if __name__ == "__main__":
    unittest.main()
