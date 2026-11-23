"""Reading a light's channels in the paired native Entertainment area (#7 stage 5b)."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge"))
from hue_area import HueAreaReader  # noqa: E402

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

    def requester(path):
        calls.append(path)
        return responses[path]

    return HueAreaReader(CONFIG, AREA, requester=requester), calls


class HueAreaReaderTests(unittest.TestCase):
    def test_a_member_light_reports_its_service_and_segment_count(self):
        hue, calls = reader()
        self.assertEqual(hue.member(LIGHT), {"service": SERVICE, "segments": 3})
        self.assertEqual(hue.member(OTHER_LIGHT), {"service": OTHER_SERVICE, "segments": 1})
        self.assertEqual(calls[0], "/clip/v2/resource/entertainment_configuration/" + AREA)

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
                HueAreaReader(config, area_id, requester=lambda path: self.fail(path))


if __name__ == "__main__":
    unittest.main()
