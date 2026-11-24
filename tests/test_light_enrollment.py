"""Owner enrollment and removal of discovered Govee lights (#7 stage 3c)."""

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge"))
import live_adapter as adapter  # noqa: E402
from hue_area import AreaBusy, AreaShared  # noqa: E402
from light_enrollment import LightEnrollment  # noqa: E402
from test_light_registry import AREA, STRIP, sample  # noqa: E402
from test_live_adapter import targets  # noqa: E402
from test_setup_studio import studio  # noqa: E402

from lights_can_communicate_v2_core.discovery import DeviceCatalog  # noqa: E402
from lights_can_communicate_v2_core.light_registry import LightRegistry  # noqa: E402

TOKEN = "a" * 48
NEW_DEVICE = "CC:CC:CC:CC:CC:CC:CC:CC"


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class EnrollmentTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory(prefix="tmp_light_enrollment_")
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "lights.json"
        self.registry = LightRegistry.from_dict(sample())
        self.registry.save(self.path)
        self.clock = Clock()
        self.catalog = DeviceCatalog(clock=self.clock, ttl=20)
        self.catalog.observe_govee({"device": NEW_DEVICE, "sku": "H6008"}, "192.168.10.52")
        self.new = self.catalog.snapshot()["devices"][0]["id"]
        self.sync = False
        self.restarts = []
        self.lights = LightEnrollment(self.path, self.registry, self.catalog,
                                      busy=lambda: self.sync,
                                      restart=lambda: self.restarts.append(True))

    def stored(self):
        return LightRegistry.load(self.path)

    def test_enrollment_saves_the_light_and_restarts_once(self):
        self.lights.enroll(self.new)
        self.assertEqual(self.restarts, [True])
        target = self.stored().govee_targets()[-1]
        self.assertEqual(target, {"light": self.new, "device": NEW_DEVICE,
                                  "ip": "192.168.10.52", "model": "H6008"})
        # Until the restart the light set does not change again.
        with self.assertRaises(adapter.LightRefused) as refused:
            self.lights.remove(self.new)
        self.assertEqual(refused.exception.reason, "restarting")

    def test_refusals_leave_the_registry_unchanged(self):
        before = self.path.read_bytes()
        self.sync = True
        for light_id, reason in ((self.new, "sync_active"),):
            with self.assertRaises(adapter.LightRefused) as refused:
                self.lights.enroll(light_id)
            self.assertEqual(refused.exception.reason, reason)
        self.sync = False
        for change, light_id, reason in (
                (self.lights.enroll, "device-" + "0" * 32, "unavailable"),
                (self.lights.remove, self.new, "not_enrolled")):
            with self.subTest(reason=reason), self.assertRaises(adapter.LightRefused) as refused:
                change(light_id)
            self.assertEqual(refused.exception.reason, reason)
        for light_id in ("govee-middle", "left", 7):
            with self.subTest(light_id=light_id), self.assertRaises(ValueError):
                self.lights.remove(light_id)
        self.clock.now += 30  # the light went offline
        with self.assertRaises(adapter.LightRefused) as refused:
            self.lights.enroll(self.new)
        self.assertEqual(refused.exception.reason, "unavailable")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.restarts, [])

    def test_a_migrated_lamp_is_removed_and_rejoins_with_its_migrated_id(self):
        device = "AA:BB:CC:DD:EE:FF:00:01"  # a Govee device id as discovery reports it
        self.lights.registry = self.registry.with_govee_targets(
            [{**target, "device": device} if target["light"] == "govee-left" else target
             for target in self.registry.govee_targets()])
        self.lights.remove("govee-left")
        self.assertEqual(self.restarts, [True])
        self.assertNotIn("govee-left", self.stored().ids)
        # After the restart discovery offers the lamp under its device id.
        catalog = DeviceCatalog(clock=self.clock, ttl=20)
        catalog.observe_govee({"device": device.lower(), "sku": "H6076"}, "192.168.10.70")
        found = catalog.snapshot()["devices"][0]["id"]
        lights = LightEnrollment(self.path, self.stored(), catalog, busy=lambda: False,
                                 restart=lambda: self.restarts.append(True))
        self.assertEqual(lights.enroll(found), "govee-left")
        light = self.stored().to_dict()["lights"][-1]
        self.assertEqual((light["id"], light["virtual"], light["govee"]["ip"]),
                         ("govee-left", {"id_v1": "1", "seed": "Left"}, "192.168.10.70"))
        self.assertNotIn(found, self.stored().ids)

    def test_sync_frames_and_light_changes_are_serialized(self):
        frames = []
        self.assertTrue(self.lights.stream(lambda: frames.append(1) or True))
        started = threading.Event()
        release = threading.Event()

        def busy():
            started.set()
            release.wait(2)
            return False

        self.lights.busy = busy
        worker = threading.Thread(target=self.lights.enroll, args=(self.new,))
        worker.start()
        started.wait(2)
        # A frame that arrives during the check waits, then sees the pending set.
        result = []
        frame = threading.Thread(target=lambda: result.append(
            self.lights.stream(lambda: frames.append(2) or True)))
        frame.start()
        frame.join(0.2)
        self.assertTrue(frame.is_alive())
        release.set()
        worker.join(2)
        frame.join(2)
        self.assertEqual((result, frames), ([False], [1]))
        self.assertEqual(self.restarts, [True])

    def test_removal_keeps_the_identity_and_stops_address_updates(self):
        enrolled = self.registry.with_govee_light(self.new, NEW_DEVICE, "192.168.10.52", "H6008")
        enrolled.save(self.path)
        lights = LightEnrollment(self.path, enrolled, self.catalog, busy=lambda: False,
                                 restart=lambda: self.restarts.append(True))
        moved = enrolled.govee_targets()
        moved[-1]["ip"] = "192.168.10.60"
        lights.remember(moved)
        self.assertEqual(self.stored().govee_targets()[-1]["ip"], "192.168.10.60")
        lights.remove(self.new)
        data = self.stored().to_dict()
        self.assertNotIn(self.new, [item["id"] for item in data["lights"]])
        self.assertEqual(data["retired"][0]["id"], self.new)
        # The running controller still has the old set; it must not write it back.
        lights.remember(moved)
        self.assertNotIn(self.new, self.stored().ids)


HUE_LIGHT = "a0000000-0000-4000-8000-00000000000a"
HUE_SERVICE = "b0000000-0000-4000-8000-00000000000b"
BRIDGE_ID = "001788FFFE123456"


class Area:
    """The paired Entertainment area of the tests, as HueArea changes it."""

    area_id = AREA

    def __init__(self, member=None, error=None, added=True):
        self.result, self.error, self.added, self.calls = member, error, added, []

    def join(self, light, replace_missing=False):
        self.calls.append(("join", light))
        self.replace_missing = replace_missing
        if self.error is not None:
            raise self.error
        return self.result, self.added

    def abandon(self, light):
        self.calls.append(("abandon", light))

    def leave(self, light):
        self.calls.append(("leave", light))
        if self.error is not None:
            raise self.error
        return ["locations before"]

    def rejoin(self, before):
        self.calls.append(("rejoin", before))


class HueEnrollmentTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory(prefix="tmp_light_enrollment_")
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "lights.json"
        self.registry = LightRegistry.from_dict(sample())
        self.registry.save(self.path)
        self.clock = Clock()
        self.catalog = DeviceCatalog(clock=self.clock, ttl=20)
        self.catalog.observe_hue(BRIDGE_ID, [
            {"id": HUE_LIGHT, "type": "light", "metadata": {"name": "Lightstrip 27"},
             "owner": {"rid": HUE_SERVICE, "rtype": "device"}}],
            [{"id": HUE_SERVICE, "product_data": {"model_id": "LCX005"}}])
        self.new = self.catalog.snapshot()["devices"][0]["id"]
        self.restarts = []

    def enrollment(self, area, registry=None, busy=lambda: False):
        return LightEnrollment(self.path, registry or self.registry, self.catalog, busy=busy,
                               restart=lambda: self.restarts.append(True), area=area)

    def test_a_hue_light_joins_the_paired_area_and_is_enrolled_with_its_channels(self):
        area = Area({"service": HUE_SERVICE, "segments": 7})
        self.enrollment(area).enroll(self.new)
        self.assertEqual(area.calls, [("join", HUE_LIGHT)])
        self.assertEqual(self.restarts, [True])
        stored = LightRegistry.load(self.path)
        light = stored.to_dict()["lights"][-1]
        self.assertEqual((light["id"], light["provider"], light["hue"]["model"]),
                         (self.new, "hue", "LCX005"))
        self.assertEqual(stored.native_profile().channels[self.new], tuple(range(12, 19)))

    def test_hue_refusals_leave_the_registry_and_the_area_unchanged(self):
        before = self.path.read_bytes()
        for area, reason, calls in (
                (None, "unavailable", None),
                (Area(error=AreaBusy()), "sync_active", [("join", HUE_LIGHT)]),
                (Area(error=AreaShared()), "channels_shared", [("join", HUE_LIGHT)]),
                (Area(error=OSError()), "unavailable", [("join", HUE_LIGHT)]),
                (Area(error=ValueError()), "unavailable", [("join", HUE_LIGHT)]),
                # Too many channels: the light that joined leaves again.
                (Area({"service": HUE_SERVICE, "segments": 9}), "area_full",
                 [("join", HUE_LIGHT), ("abandon", HUE_LIGHT)]),
                # A light that was already a member stays there.
                (Area({"service": HUE_SERVICE, "segments": 9}, added=False), "area_full",
                 [("join", HUE_LIGHT)])):
            with self.subTest(reason=reason, calls=calls), \
                    self.assertRaises(adapter.LightRefused) as refused:
                self.enrollment(area).enroll(self.new)
            self.assertEqual(refused.exception.reason, reason)
            if area is not None:
                self.assertEqual(area.calls, calls)
        area = Area({"service": HUE_SERVICE, "segments": 1})
        with self.assertRaises(adapter.LightRefused) as refused:
            self.enrollment(area, busy=lambda: True).enroll(self.new)
        self.assertEqual((refused.exception.reason, area.calls), ("sync_active", []))
        self.clock.now += 30  # the light went offline
        with self.assertRaises(adapter.LightRefused) as refused:
            self.enrollment(area).enroll(self.new)
        self.assertEqual((refused.exception.reason, area.calls), ("unavailable", []))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.restarts, [])

    def test_a_light_that_goes_offline_while_it_joins_is_refused_and_leaves(self):
        catalog = self.catalog

        class Offline(Area):
            def join(self, light, replace_missing=False):
                result = super().join(light, replace_missing)
                catalog._entries[next(iter(catalog._entries))]["seen"] = -100
                return result

        area = Offline({"service": HUE_SERVICE, "segments": 1})
        with self.assertRaises(adapter.LightRefused) as refused:
            self.enrollment(area).enroll(self.new)
        self.assertEqual(refused.exception.reason, "unavailable")
        self.assertEqual(area.calls, [("join", HUE_LIGHT), ("abandon", HUE_LIGHT)])
        self.assertEqual(self.restarts, [])

    def test_the_first_hue_light_sets_the_area_that_the_core_created(self):
        registry = self.registry
        for item in self.registry.to_dict()["lights"]:
            if item["provider"] == "hue":
                registry = registry.without_light(item["id"])
        registry.save(self.path)
        new_area = "f1111111-1111-4111-8111-111111111111"

        class Created(Area):
            area_id = None

            def join(self, light, replace_missing=False):
                result = super().join(light, replace_missing)
                self.area_id = new_area
                return result

        area = Created({"service": HUE_SERVICE, "segments": 7})
        self.enrollment(area, registry).enroll(self.new)
        # No Hue light depended on the old area, so a lost one may be replaced.
        self.assertTrue(area.replace_missing)
        stored = LightRegistry.load(self.path)
        self.assertEqual(stored.to_dict()["hue"], {"area_id": new_area})
        self.assertEqual(stored.native_profile().channels[self.new], tuple(range(7)))
        # With Hue lights enrolled, their area is never replaced.
        with_lights = Area({"service": HUE_SERVICE, "segments": 1})
        self.enrollment(with_lights).enroll(self.new)
        self.assertFalse(with_lights.replace_missing)

    def test_concurrent_enrollments_of_one_light_change_the_area_once(self):
        joining, release = threading.Event(), threading.Event()

        class Slow(Area):
            def join(self, light, replace_missing=False):
                joining.set()
                release.wait(5)
                return super().join(light, replace_missing)

        area = Slow({"service": HUE_SERVICE, "segments": 1})
        lights = self.enrollment(area)
        results = []

        def enroll():
            try:
                lights.enroll(self.new)
                results.append("enrolled")
            except adapter.LightRefused as refused:
                results.append(refused.reason)

        first = threading.Thread(target=enroll)
        first.start()
        joining.wait(5)
        second = threading.Thread(target=enroll)
        second.start()
        release.set()
        first.join(5)
        second.join(5)
        # The second request waits for the first and then finds the light enrolled
        # (or the adapter restarting); it never undoes the first one's area change.
        self.assertEqual(results[0], "enrolled")
        self.assertIn(results[1], ("enrolled", "restarting"))
        self.assertEqual(area.calls, [("join", HUE_LIGHT)])

    def test_an_enrolled_hue_light_leaves_the_area_and_keeps_its_identity(self):
        self.enrollment(Area({"service": HUE_SERVICE, "segments": 2})).enroll(self.new)
        enrolled = self.path.read_bytes()
        for area, reason in ((None, "unavailable"), (Area(error=AreaBusy()), "sync_active"),
                             (Area(error=OSError()), "unavailable")):
            with self.subTest(reason=reason), self.assertRaises(adapter.LightRefused) as refused:
                self.enrollment(area, LightRegistry.load(self.path)).remove(self.new)
            self.assertEqual(refused.exception.reason, reason)
        self.assertEqual(self.path.read_bytes(), enrolled)
        # A registry that cannot be written puts the light back into the area.
        area = Area()
        lights = self.enrollment(area, LightRegistry.load(self.path))
        # A directory in the registry's place cannot be replaced, even by root.
        lights.path = self.path.parent / "taken"
        (lights.path / "entry").mkdir(parents=True)
        with self.assertRaises(OSError):
            lights.remove(self.new)
        self.assertEqual(area.calls, [("leave", HUE_LIGHT), ("rejoin", ["locations before"])])
        self.assertEqual(self.path.read_bytes(), enrolled)
        area = Area()
        self.enrollment(area, LightRegistry.load(self.path)).remove(self.new)
        self.assertEqual(area.calls, [("leave", HUE_LIGHT)])
        data = LightRegistry.load(self.path).to_dict()
        self.assertEqual([item["id"] for item in data["lights"]],
                         [item["id"] for item in sample()["lights"]])
        self.assertEqual(data["retired"], [{"id": self.new,
                                            "virtual": {"id_v1": "7", "seed": self.new},
                                            "identity": {"provider": "hue",
                                                         "key": HUE_LIGHT}}])

    def test_the_last_hue_light_can_go_and_the_paired_area_stays(self):
        registry = LightRegistry.from_dict(sample())
        for light in ("hue-monitor", "play-left", "play-center"):
            registry = registry.without_light(light)
        area = Area()
        self.enrollment(area, registry).remove("play-right")
        self.assertEqual(area.calls, [("leave", "d4444444-4444-4444-8444-444444444444")])
        stored = LightRegistry.load(self.path)
        self.assertIsNone(stored.native_profile())
        self.assertEqual(stored.to_dict()["hue"], {"area_id": AREA})

    def test_a_migrated_hue_lamp_leaves_the_area(self):
        area = Area()
        self.enrollment(area).remove("hue-monitor")
        self.assertEqual(area.calls, [("leave", STRIP)])
        self.assertEqual(self.restarts, [True])
        profile = LightRegistry.load(self.path).native_profile()
        self.assertEqual(profile.channels, {"play-center": (0,), "play-left": (1,),
                                            "play-right": (2,)})


class AdapterRouteTests(unittest.TestCase):
    def setUp(self):
        self.calls, self.answer = [], None

        class Lights:
            def enroll(inner, light_id):
                self.calls.append(("enroll", light_id))
                if self.answer is not None:
                    raise self.answer

            def remove(inner, light_id):
                self.calls.append(("remove", light_id))
                if self.answer is not None:
                    raise self.answer

        controller = adapter.GoveeController(targets(), sender=lambda *args: True)
        handler = adapter.handler_for(controller, TOKEN, lights=Lights())
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.addCleanup(self.server.server_close)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def call(self, path, body, token=TOKEN):
        request = urllib.request.Request(self.url + path, data=json.dumps(body).encode(),
                                         headers={"X-Adapter-Token": token})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=5) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            with error:
                return error.code, json.load(error)

    def test_routes_forward_the_id_and_report_the_restart(self):
        light = "device-" + "c" * 32
        self.assertEqual(self.call("/lights/enroll", {"id": light}),
                         (200, {"accepted": True, "restarting": True}))
        self.assertEqual(self.call("/lights/remove", {"id": light})[0], 200)
        self.assertEqual(self.calls, [("enroll", light), ("remove", light)])

    def test_refusals_errors_and_authentication(self):
        light = "device-" + "c" * 32
        self.answer = adapter.LightRefused("sync_active")
        self.assertEqual(self.call("/lights/enroll", {"id": light}),
                         (409, {"accepted": False, "reason": "sync_active"}))
        self.answer = OSError("disk")
        self.assertEqual(self.call("/lights/enroll", {"id": light}),
                         (503, {"accepted": False}))
        self.answer = ValueError("bad id")
        self.assertEqual(self.call("/lights/remove", {"id": "govee-left"})[0], 400)
        self.calls.clear()
        self.assertEqual(self.call("/lights/enroll", {"id": light, "x": 1})[0], 400)
        self.assertEqual(self.call("/lights/enroll", {"id": light}, token="b" * 48)[0], 403)
        self.assertEqual(self.calls, [])

    def test_without_enrollment_the_routes_do_not_exist(self):
        controller = adapter.GoveeController(targets(), sender=lambda *args: True)
        server = ThreadingHTTPServer(("127.0.0.1", 0), adapter.handler_for(controller, TOKEN))
        self.addCleanup(server.server_close)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        self.url = f"http://127.0.0.1:{server.server_address[1]}"
        self.assertEqual(self.call("/lights/enroll", {"id": "device-" + "c" * 32})[0], 404)


class RegistryLightsTests(unittest.TestCase):
    def test_every_enrolled_light_is_listed_with_its_zone_count(self):
        govee, hue = "device-" + "c" * 32, "device-" + "e" * 32
        registry = LightRegistry.from_dict(sample()).with_govee_light(
            govee, "CC:CC:CC:CC:CC:CC:CC:CC", "192.168.10.52", "H6008").with_hue_light(
            hue, AREA, HUE_LIGHT, HUE_SERVICE, 7, "LCX005")

        class Controller:
            by_light = {"govee-left": {"segment_count": 7}, "govee-right": {"segment_count": 0},
                        govee: {"segment_count": 0}}

        lights = {item["id"]: item for item in adapter.registry_lights(registry, Controller())}
        self.assertEqual(list(lights)[:6], [item["id"] for item in sample()["lights"]])
        self.assertEqual(lights["govee-left"]["zones"], 7)
        self.assertEqual(lights["govee-right"]["zones"], 1)
        self.assertEqual(lights["hue-monitor"]["zones"], 9)
        self.assertEqual(lights[govee], {"id": govee, "provider": "govee", "model": "H6008",
                                         "zones": 1, "migrated": False})
        self.assertEqual(lights[hue], {"id": hue, "provider": "hue", "model": "LCX005",
                                       "zones": 7, "migrated": False})
        self.assertTrue(lights["play-left"]["migrated"])
        # No device identity or address reaches the list.
        self.assertNotIn("CC:CC", json.dumps(list(lights.values())))
        self.assertNotIn("192.168", json.dumps(list(lights.values())))


class StudioRouteTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory(prefix="tmp_studio_lights_")
        self.addCleanup(temp.cleanup)
        folder = Path(temp.name)
        self.calls = []
        self.answer = (200, {"accepted": True, "restarting": True})

        def post(route, body):
            self.calls.append((route, body))
            if isinstance(self.answer, Exception):
                raise self.answer
            return self.answer

        server = ThreadingHTTPServer(("127.0.0.1", 0), studio.handler_for(
            folder / "missing-token", folder / "desk-layout.json", lights_post=post))
        worker = threading.Thread(target=server.serve_forever)
        worker.start()
        self.base = "http://127.0.0.1:" + str(server.server_port)

        def close():
            server.shutdown()
            server.server_close()
            worker.join(2)

        self.addCleanup(close)

    def post(self, path, body, **headers):
        request = urllib.request.Request(
            self.base + path, data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json", "Origin": self.base, **headers})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=5) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as response:
            with response:
                return response.code, json.load(response)

    def test_enroll_and_remove_reach_the_adapter(self):
        light = "device-" + "c" * 32
        self.assertEqual(self.post("/api/lights/enroll", {"id": light}),
                         (200, {"accepted": True, "restarting": True}))
        self.assertEqual(self.post("/api/lights/remove", {"id": light})[0], 200)
        self.assertEqual(self.calls, [("/lights/enroll", {"id": light}),
                                      ("/lights/remove", {"id": light})])

    def test_a_migrated_lamp_is_removed_and_the_enrolled_id_is_passed_on(self):
        self.assertEqual(self.post("/api/lights/remove", {"id": "play-center"})[0], 200)
        self.answer = (200, {"accepted": True, "restarting": True, "id": "play-center"})
        light = "device-" + "c" * 32
        self.assertEqual(self.post("/api/lights/enroll", {"id": light}),
                         (200, {"accepted": True, "restarting": True, "id": "play-center"}))
        self.answer = (200, {"accepted": True, "restarting": True, "id": "../secret"})
        self.assertEqual(self.post("/api/lights/enroll", {"id": light})[1],
                         {"accepted": True, "restarting": True})
        self.assertEqual(self.calls[:2], [("/lights/remove", {"id": "play-center"}),
                                          ("/lights/enroll", {"id": light})])

    def test_only_known_reasons_and_valid_requests_pass(self):
        light = "device-" + "c" * 32
        self.answer = (409, {"accepted": False, "reason": "unavailable"})
        self.assertEqual(self.post("/api/lights/enroll", {"id": light}),
                         (409, {"accepted": False, "reason": "unavailable"}))
        self.answer = (409, {"accepted": False, "reason": "secret detail"})
        self.assertEqual(self.post("/api/lights/enroll", {"id": light})[1]["reason"], "refused")
        self.answer = OSError("down")
        self.assertEqual(self.post("/api/lights/enroll", {"id": light})[0], 503)
        self.calls.clear()
        for body in ({"id": "govee-left"}, {"id": light, "x": 1}, {"lamp": light}):
            with self.subTest(body=body):
                self.assertEqual(self.post("/api/lights/enroll", body)[0], 400)
        self.assertEqual(self.post("/api/lights/enroll", {"id": light},
                                   Origin="http://evil.example")[0], 403)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
