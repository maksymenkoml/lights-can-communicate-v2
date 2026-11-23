"""A light's membership in the paired native Entertainment area (#7 stage 5b)."""

import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge"))
from hue_area import AreaBusy, AreaShared, HueArea  # noqa: E402

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
        "id": AREA, "type": "entertainment_configuration",
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
    def test_a_member_light_reports_its_service_and_segment_count(self):
        hue, calls = reader()
        self.assertEqual(hue.member(LIGHT), {"service": SERVICE, "segments": 3})
        self.assertEqual(hue.member(OTHER_LIGHT), {"service": OTHER_SERVICE, "segments": 1})
        self.assertEqual(calls[0], "/clip/v2/resource/entertainment_configuration/" + AREA)

    def test_resource_ids_are_compared_and_returned_in_canonical_form(self):
        upper = {"errors": [], "data": [
            {"id": SERVICE.upper(), "renderer_reference": {"rid": LIGHT.upper(),
                                                           "rtype": "light"}}]}
        hue, _ = reader(area(channels=[channel(0, SERVICE.upper(), 0)]), upper)
        self.assertEqual(hue.member(LIGHT.upper()), {"service": SERVICE, "segments": 1})

    def test_a_light_outside_the_area_is_not_a_member(self):
        hue, calls = reader(area(lights=(OTHER_LIGHT,)))
        self.assertIsNone(hue.member(LIGHT))
        self.assertIsNone(hue.member("not-a-uuid"))
        # The services are not read for a light outside the area.
        self.assertEqual(len(calls), 1)

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
                    hue.member(LIGHT)

    def test_configuration_is_checked_without_io(self):
        for config, area_id in (({}, AREA), ({"bridge": "192.168.10.20"}, AREA),
                                (CONFIG, "not-an-area"), (CONFIG, None)):
            with self.subTest(config=config, area_id=area_id), self.assertRaises(ValueError):
                HueArea(config, area_id, requester=lambda *call: self.fail(call))


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
        # A member that shares channels cannot be read either.
        shared = Bridge((OTHER_SERVICE, SERVICE), shared=True)
        with self.assertRaises(AreaShared):
            HueArea(CONFIG, AREA, requester=shared).member(LIGHT)

    def test_leave_removes_only_the_light(self):
        bridge = Bridge((OTHER_SERVICE, SERVICE))
        hue = HueArea(CONFIG, AREA, requester=bridge)
        hue.leave(LIGHT)
        self.assertEqual(bridge.writes, [{"locations": {"service_locations": Bridge().rows}}])
        self.assertIsNone(hue.member(LIGHT))
        hue.leave(LIGHT)
        self.assertEqual(len(bridge.writes), 1)

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


if __name__ == "__main__":
    unittest.main()
