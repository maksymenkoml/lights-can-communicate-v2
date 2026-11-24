"""Off during Sync and Blink for the six role lamps, with fakes only."""

import copy
import importlib.util
import json
import os
import stat
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge"))
from desk_layout import LayoutStore, composed, validate_layout  # noqa: E402
from entertainment_stream import VideoLease  # noqa: E402
from govee_segments import razer_colors  # noqa: E402
from homekit_power import HomeKitPower  # noqa: E402
from hue_strip_proxy import HueStripProxy, PinnedHueClient  # noqa: E402
from identify import Identify, IdentifyFailed, IdentifyRefused  # noqa: E402
from live_adapter import GoveeController, handler_for  # noqa: E402
from power_owner import PowerOwner  # noqa: E402
from sync_off import STATE_FILE, SyncOffSession  # noqa: E402
from test_composition_output import envelope  # noqa: E402
from test_desk_layout import layout  # noqa: E402
from test_homekit_power import FakeLamps  # noqa: E402
from test_hue_strip_proxy import FakeHue  # noqa: E402
from test_live_adapter import LEFT, RIGHT, command, targets  # noqa: E402
from test_native_profile import PLAY_IDS, profile_data  # noqa: E402
from test_video_output import FakeProxy  # noqa: E402
from video_output import VideoOutput  # noqa: E402

from lights_can_communicate_v2_core.native_profile import NativeHueProfile  # noqa: E402

# Every role lamp reaches the adapter by its registry light id.
LAMPS = {"govee-left": "govee-left", "govee-right": "govee-right",
         "hue-monitor": "hue-monitor", "play-left": "play-left", "play-center": "play-center",
         "play-right": "play-right"}


def with_roles(**roles):
    data = layout()
    data["version"] = 5
    for row in data["devices"]:
        if row["id"] in roles:
            row["syncRole"] = roles[row["id"]]
    return data


class PowerHue(FakeHue):
    """A native Hue light whose power follows writes; identify is recorded."""

    def __init__(self, on=True):
        super().__init__()
        self.native["on"]["on"] = on
        self.power_calls, self.identified = [], 0
        self.fail_power = False

    def power(self, on):
        if self.fail_power:
            raise OSError("bridge unreachable")
        self.power_calls.append(on)
        self.native["on"]["on"] = on

    def identify(self):
        self.identified += 1


class LayoutOffTests(unittest.TestCase):
    def test_off_is_accepted_and_stored_for_each_role_row_only(self):
        for key in LAMPS:
            with self.subTest(key=key):
                result = validate_layout(with_roles(**{key: "off"}))
                row = next(item for item in result["devices"] if item["id"] == key)
                self.assertEqual(row["syncRole"], "off")
                self.assertEqual(result["version"], 7)
                self.assertEqual(validate_layout(copy.deepcopy(result)), result)
        for identity, extra in (
            ("virtual-lamp1", {"shape": "floor", "name": "Spare"}),
            ("device-" + "a" * 32, {"name": "Third", "model": "H6076", "shape": "floor",
                                    "zones": 7}),
        ):
            data = with_roles()
            data["devices"].append({"id": identity, "scale": 80, "orientation": "horizontal",
                                    "positionCm": {"x": 100, "height": 60}, **extra})
            for role in ("own", "sync"):
                data["devices"][-1]["syncRole"] = role
                validate_layout(data)
            data["devices"][-1]["syncRole"] = "off"
            with self.subTest(identity=identity), self.assertRaises(ValueError):
                validate_layout(data)

    def test_off_is_outside_sync_but_present_for_scenes(self):
        self.assertFalse(composed({"id": "govee-left", "syncRole": "off"}))
        self.assertFalse(composed({"id": "govee-left", "syncRole": "own"}))
        self.assertTrue(composed({"id": "govee-left"}))
        with TemporaryDirectory(prefix="tmp_lamp_controls_layout_") as folder:
            store = LayoutStore(Path(folder) / "desk-layout.json")
            self.assertEqual(store.sync_off_roles(), [])
            data = with_roles(**{"govee-left": "off", "hue-monitor": "off",
                                 "play-left": "own"})
            data["devices"][4]["enabled"] = False
            data["devices"][4]["syncRole"] = "off"
            store.save(data)
            self.assertFalse(store.enabled("govee-left"))
            self.assertTrue(store.present("govee-left"))
            self.assertFalse(store.enabled("hue-monitor"))
            self.assertTrue(store.present("hue-monitor"))
            profile = store.sync_profile()
            self.assertEqual(profile["enabled_roles"], ["right", "play-right"])
            self.assertEqual(profile["native_roles"], ["play-right"])
            # A removed row is never switched, even when it says "off".
            self.assertEqual(store.sync_off_roles(), ["govee-left", "hue-monitor"])


class SessionFixture(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory(prefix="tmp_sync_off_")
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.state = self.folder / STATE_FILE
        self.store = LayoutStore(self.folder / "desk-layout.json")
        self.store.save(with_roles(**{"govee-left": "off", "hue-monitor": "off"}))
        self.lamps = FakeLamps()
        self.hue = PowerHue()
        self.strip = HueStripProxy(self.hue, "hue-monitor", clock=self.lamps.clock,
                                   wall_clock=lambda: 1000)
        self.strip.process_once()
        self.active = False

    def homekit(self):
        power = HomeKitPower(targets(), self.folder / "homekit-power.json", self.lamps.send,
                             self.lamps.observe, clock=self.lamps.clock, sleep=self.lamps.sleep)
        for side in ("govee-left", "govee-right"):
            self.assertTrue(power.set({"light": side, "on": True}))
        return power

    def owner(self):
        gate = PowerOwner(targets(), clock=self.lamps.clock, wall_clock=lambda: 1000)
        gate.attach_power(self.lamps.send, self.lamps.observe, sleep=self.lamps.sleep)
        self.sequence = 0
        self.grant(gate, left=True, right=True)
        return gate

    def grant(self, gate, **states):
        self.sequence += 1
        return gate.submit({"session": "home", "sequence": self.sequence, "issued_at": 1000,
                            "states": {"govee-" + side: value for side, value in states.items()}})

    def session(self, govee):
        return SyncOffSession(self.store, lambda: self.active, self.state, govee=govee,
                              native={"hue-monitor": self.strip}, observe=self.lamps.observe,
                              clock=self.lamps.clock)

    def turns(self, side):
        return [data["value"] for name, command_name, data in self.lamps.sent
                if name == side and command_name == "turn"]


class SyncOffSessionTests(SessionFixture):
    def test_homekit_session_switches_off_and_restores_without_changing_intent(self):
        power = self.homekit()
        session = self.session(power)
        self.lamps.sent.clear()
        session.step()
        self.assertEqual(self.turns("govee-left"), [])
        self.active = True
        session.step()
        self.assertEqual(self.turns("govee-left"), [0])
        self.assertEqual(self.turns("govee-right"), [])
        self.assertFalse(self.lamps.native["govee-left"])
        self.assertFalse(power.allowed("govee-left"))
        self.assertTrue(power.allowed("govee-right"))
        self.assertEqual(self.hue.power_calls, [False])
        # HomeKit keeps showing the owner's intent, and tick() never adopts the hold.
        for _ in range(40):
            self.lamps.now += 0.5
            power.tick()
            session.step()
        roles = power.state()["lights"]
        self.assertIs(roles["govee-left"]["on"], True)
        self.assertIs(roles["govee-left"]["requested_on"], True)
        self.assertIs(roles["govee-left"]["native_on"], False)
        self.assertTrue(power.snapshot()["lights"]["govee-left"]["sync_hold"])
        self.assertTrue(power.snapshot()["lights"]["govee-left"]["requested_on"])
        saved = json.loads(self.state.read_text())
        self.assertEqual(saved, {"version": 1, "roles": ["govee-left", "hue-monitor"]})
        self.assertEqual(stat.S_IMODE(os.stat(self.state).st_mode), 0o600)
        self.active = False
        session.step()
        self.assertEqual(self.turns("govee-left"), [0, 1])
        self.assertTrue(self.lamps.native["govee-left"])
        self.assertTrue(power.allowed("govee-left"))
        self.assertEqual(self.hue.power_calls, [False, True])
        self.assertFalse(self.state.exists())
        self.assertEqual(session.snapshot()["roles"], [])

    def test_lamps_already_off_stay_off(self):
        power = self.homekit()
        self.assertTrue(power.set({"light": "govee-left", "on": False}))
        self.hue.native["on"]["on"] = False
        self.strip.process_once()
        session = self.session(power)
        self.lamps.sent.clear()
        self.active = True
        session.step()
        self.assertFalse(power.sync_held("govee-left"))
        self.assertFalse(self.state.exists())
        self.active = False
        session.step()
        self.assertEqual(self.turns("govee-left"), [])
        self.assertEqual(self.hue.power_calls, [])
        self.assertFalse(self.lamps.native["govee-left"])

    def test_owner_changes_during_the_session_win(self):
        power = self.homekit()
        session = self.session(power)
        self.active = True
        session.step()
        # HomeKit Off during Sync: intent becomes Off and nothing is restored.
        self.assertTrue(power.set({"light": "govee-left", "on": False}))
        self.assertFalse(power.sync_held("govee-left"))
        # Hue app On through the virtual bridge's native power route.
        session.owner_power("hue-monitor")
        self.assertEqual(json.loads(self.state.read_text())["roles"], ["govee-left"])
        # The watcher drops the Govee record once the authority dropped its hold.
        session.step()
        self.assertFalse(self.state.exists())
        self.lamps.sent.clear()
        self.active = False
        session.step()
        self.assertEqual(self.turns("govee-left"), [])
        self.assertFalse(self.lamps.native["govee-left"])
        self.assertEqual(self.hue.power_calls, [False])

    def test_owner_on_during_the_session_turns_the_lamp_on_and_keeps_it(self):
        power = self.homekit()
        session = self.session(power)
        self.active = True
        session.step()
        self.assertTrue(power.set({"light": "govee-left", "on": True}))
        self.assertTrue(self.lamps.native["govee-left"])
        self.assertTrue(power.allowed("govee-left"))
        # The physical Hue app switched the strip on directly at the bridge.
        self.hue.native["on"]["on"] = True
        self.strip.process_once()
        session.step()
        self.assertEqual(session.snapshot()["roles"], [])
        self.active = False
        self.lamps.sent.clear()
        session.step()
        self.assertEqual(self.turns("govee-left"), [])
        self.assertEqual(self.hue.power_calls, [False])

    def test_role_changes_during_the_session_apply_at_once(self):
        power = self.homekit()
        self.store.save(with_roles())
        session = self.session(power)
        self.lamps.sent.clear()
        self.active = True
        session.step()
        self.assertEqual(session.snapshot()["roles"], [])
        # Set to Off while Sync runs: both lamps go off on the next step.
        self.store.save(with_roles(**{"govee-left": "off", "hue-monitor": "off"}))
        session.step()
        self.assertEqual(self.turns("govee-left"), [0])
        self.assertEqual(self.hue.power_calls, [False])
        self.assertEqual(session.snapshot()["roles"], ["govee-left", "hue-monitor"])
        # The owner switches the left lamp on again: it is not switched off again.
        self.assertTrue(power.set({"light": "govee-left", "on": True}))
        for _ in range(5):
            self.lamps.now += 0.5
            power.tick()
            session.step()
        self.assertEqual(self.turns("govee-left"), [0, 1])
        self.assertEqual(session.snapshot()["roles"], ["hue-monitor"])
        # The strip leaves Off while Sync runs: its power comes back at once.
        self.store.save(with_roles(**{"govee-left": "off"}))
        session.step()
        self.assertEqual(self.hue.power_calls, [False, True])
        self.assertEqual(session.snapshot()["roles"], [])
        self.assertFalse(self.state.exists())
        # Set to Off again in the same session: it is switched off again.
        self.store.save(with_roles(**{"govee-left": "off", "hue-monitor": "off"}))
        session.step()
        self.assertEqual(self.hue.power_calls, [False, True, False])
        self.assertEqual(self.turns("govee-left"), [0, 1])
        # Session end restores what is still recorded, and only that.
        self.active = False
        session.step()
        self.assertEqual(self.hue.power_calls, [False, True, False, True])
        self.assertEqual(self.turns("govee-left"), [0, 1])

    def test_failed_restore_of_a_role_leaving_off_is_retried_during_the_session(self):
        session = self.session(None)
        self.active = True
        session.step()
        self.hue.fail_power = True
        self.store.save(with_roles(**{"govee-left": "off"}))
        session.step()
        self.assertEqual(session.snapshot()["roles"], ["hue-monitor"])
        self.hue.fail_power = False
        session.step()
        self.assertEqual(session.snapshot()["roles"], ["hue-monitor"], "retry waits a second")
        self.lamps.now += 1.5
        session.step()
        self.assertEqual(session.snapshot()["roles"], [])
        self.assertTrue(self.hue.native["on"]["on"])

    def test_a_failed_restore_does_not_delay_another_lamp_leaving_off(self):
        power = self.homekit()
        session = self.session(power)
        self.active = True
        session.step()
        self.assertEqual(session.snapshot()["roles"], ["govee-left", "hue-monitor"])
        self.hue.fail_power = True
        self.store.save(with_roles(**{"govee-left": "off"}))
        session.step()
        self.assertEqual(session.snapshot()["roles"], ["govee-left", "hue-monitor"])
        # The left lamp leaves Off on the next step: restored at once.
        self.store.save(with_roles())
        session.step()
        self.assertEqual(session.snapshot()["roles"], ["hue-monitor"])
        self.assertFalse(power.sync_held("govee-left"))
        self.assertTrue(self.lamps.native["govee-left"])

    def test_power_owner_hold_closes_the_gate_and_owner_off_wins(self):
        gate = self.owner()
        session = self.session(gate)
        self.active = True
        session.step()
        self.assertEqual(self.turns("govee-left"), [0])
        self.assertFalse(gate.allowed("govee-left"))
        self.assertTrue(gate.allowed("govee-right"))
        # Owner heartbeats that keep the same intent keep the hold.
        self.assertTrue(self.grant(gate, left=True, right=True))
        self.assertFalse(gate.allowed("govee-left"))
        self.assertTrue(gate.snapshot()["lights"]["govee-left"]["requested_on"])
        self.active = False
        session.step()
        self.assertEqual(self.turns("govee-left"), [0, 1])
        self.assertTrue(gate.allowed("govee-left"))
        # Second session: the owner switches the lamp off in HomeKit during Sync.
        self.active = True
        session.step()
        self.assertTrue(self.grant(gate, left=False, right=True))
        self.assertFalse(gate.sync_held("govee-left"))
        session.step()
        self.assertNotIn("govee-left", session.snapshot()["roles"])
        self.active = False
        session.step()
        self.assertEqual(self.turns("govee-left"), [0, 1, 0])
        self.assertFalse(self.lamps.native["govee-left"])

    def test_without_a_power_authority_govee_is_untouched(self):
        session = self.session(None)
        self.active = True
        session.step()
        self.assertEqual(self.lamps.sent, [])
        self.assertEqual(session.snapshot()["roles"], ["hue-monitor"])
        self.active = False
        session.step()
        self.assertEqual(self.hue.power_calls, [False, True])

    def test_moving_off_to_another_lamp_during_the_session_moves_the_hold(self):
        power = self.homekit()
        session = self.session(power)
        self.active = True
        session.step()
        self.store.save(with_roles(**{"govee-right": "off"}))
        session.step()
        self.assertFalse(power.sync_held("govee-left"))
        self.assertTrue(self.lamps.native["govee-left"])
        self.assertTrue(power.sync_held("govee-right"))
        self.assertFalse(self.lamps.native["govee-right"])
        self.assertEqual(self.hue.power_calls, [False, True])
        self.active = False
        session.step()
        self.assertFalse(power.sync_held("govee-right"))
        self.assertTrue(self.lamps.native["govee-right"])
        self.assertEqual(self.hue.power_calls, [False, True])

    def test_a_lamp_is_left_alone_when_its_record_cannot_be_saved(self):
        power = self.homekit()
        self.state = self.folder / "missing" / STATE_FILE
        session = self.session(power)
        self.lamps.sent.clear()
        self.active = True
        session.step()
        self.assertEqual(self.turns("govee-left"), [], "no Off without a recovery record")
        self.assertEqual(self.hue.power_calls, [])
        self.assertTrue(power.allowed("govee-left"))
        self.assertEqual(session.snapshot()["roles"], [])

    def test_failed_govee_restore_stays_held_recorded_and_is_retried(self):
        for gate in (self.homekit(), self.owner()):
            with self.subTest(gate=type(gate).__name__):
                self.lamps.native["govee-left"] = True
                session = self.session(gate)
                self.active = True
                session.step()
                self.assertFalse(self.lamps.native["govee-left"])
                self.lamps.ignore.add("govee-left")
                self.active = False
                session.step()
                self.assertIn("govee-left", json.loads(self.state.read_text())["roles"])
                self.assertFalse(gate.allowed("govee-left"),
                                 "the hold stays until the lamp is back")
                self.lamps.ignore.discard("govee-left")
                self.lamps.now += 2
                session.step()
                self.assertTrue(self.lamps.native["govee-left"])
                self.assertNotIn("govee-left", gate.holds, "a confirmed restore ends the hold")
                self.assertNotIn("govee-left", session.snapshot()["roles"])
                session.close()

    def test_failed_native_restore_is_retried_and_kept_on_disk(self):
        session = self.session(None)
        self.active = True
        session.step()
        self.hue.fail_power = True
        self.active = False
        session.step()
        self.assertEqual(json.loads(self.state.read_text())["roles"], ["hue-monitor"])
        self.hue.fail_power = False
        self.lamps.now += 2
        session.step()
        self.assertFalse(self.state.exists())
        self.assertTrue(self.hue.native["on"]["on"])

    def test_crash_file_is_restored_on_start_and_deleted(self):
        power = self.homekit()
        self.lamps.native["govee-left"] = False
        self.hue.native["on"]["on"] = False
        self.strip.process_once()
        self.state.write_text(json.dumps({"version": 1, "roles": ["govee-left", "hue-monitor"]}))
        restarted = HomeKitPower(targets(), self.folder / "homekit-power.json", self.lamps.send,
                                 self.lamps.observe, clock=self.lamps.clock,
                                 sleep=self.lamps.sleep)
        self.assertIsNot(restarted, power)
        session = self.session(restarted)
        # The crash-time Off is held, so tick() cannot adopt it before the restore.
        self.assertTrue(restarted.sync_held("govee-left"))
        for _ in range(20):
            self.lamps.now += 0.5
            restarted.tick()
        self.assertTrue(restarted.state()["lights"]["govee-left"]["requested_on"])
        session.step()
        self.assertTrue(self.lamps.native["govee-left"])
        self.assertTrue(restarted.allowed("govee-left"))
        self.assertTrue(self.hue.native["on"]["on"])
        self.assertFalse(self.state.exists())

    def test_crash_file_with_legacy_role_names_is_restored(self):
        power = self.homekit()
        self.lamps.native["govee-left"] = False
        self.hue.native["on"]["on"] = False
        self.strip.process_once()
        # A file written before the registry ids names the lamps by side and native role.
        self.state.write_text(json.dumps({"version": 1, "roles": ["left", "strip"]}))
        session = self.session(power)
        self.assertEqual(session.snapshot()["roles"], ["govee-left", "hue-monitor"])
        self.lamps.now += 2
        session.step()
        self.assertTrue(self.lamps.native["govee-left"])
        self.assertTrue(self.hue.native["on"]["on"])
        self.assertFalse(self.state.exists())

    def test_crash_restore_waits_for_a_native_reading(self):
        power = self.homekit()
        self.lamps.native["govee-left"] = False
        self.lamps.reachable["govee-left"] = False
        self.state.write_text(json.dumps({"version": 1, "roles": ["govee-left"]}))
        session = self.session(power)
        self.lamps.sent.clear()
        session.step()
        self.assertEqual(self.turns("govee-left"), [])
        self.assertTrue(self.state.exists())
        self.lamps.reachable["govee-left"] = True
        self.lamps.now += 2
        session.step()
        self.assertEqual(self.turns("govee-left"), [1])
        self.assertFalse(self.state.exists())

    def test_shutdown_restores_immediately(self):
        power = self.homekit()
        session = self.session(power)
        self.active = True
        session.step()
        session.close()
        self.assertTrue(self.lamps.native["govee-left"])
        self.assertTrue(self.hue.native["on"]["on"])
        self.assertFalse(self.state.exists())
        session.step()
        self.assertEqual(self.turns("govee-left"), [1, 0, 1])


class FenceTests(unittest.TestCase):
    def test_an_off_native_role_does_not_trip_the_sync_safety_fence(self):
        with TemporaryDirectory(prefix="tmp_sync_off_fence_") as folder:
            store = LayoutStore(Path(folder) / "desk-layout.json")
            store.save(with_roles(**{"hue-monitor": "off"}))
            now = [10]
            controller = GoveeController(targets(), sender=lambda *args: True,
                                         clock=lambda: now[0], wall_clock=lambda: 1000,
                                         layout_store=store)
            proxies = []
            for light in ("hue-monitor", "play-left", "play-center", "play-right"):
                proxy = FakeProxy()
                proxy.light = light
                proxies.append(proxy)
            # Sync switched the strip off: fresh, healthy and native Off.
            proxies[0].value["native_on"] = False

            class Relay:
                frames = []

                def start(self, profile=None):
                    return True

                def send(self, frame, projected=None):
                    self.frames.append(frame)

                def stop(self):
                    pass

            lease = VideoLease(clock=lambda: now[0], wall_clock=lambda: 1000)
            output = VideoOutput(controller, Relay(), proxies, lease=lease,
                                 clock=lambda: now[0])
            lease.submit(envelope())
            self.assertTrue(output.process_once())
            self.assertTrue(output.safe())
            self.assertFalse(output.fresh_off())
            self.assertIsNone(output.last_safety_failure)
            self.assertEqual(output.last_result, "streaming")


class IdentifyFixture(unittest.TestCase):
    def setUp(self):
        self.now = 10
        self.sent = []
        self.controller = GoveeController(
            targets(), sender=lambda device, name, data: self.sent.append(
                ("scene", device["light"], name, data["value"] if name == "brightness" else None))
            or True, clock=lambda: self.now, wall_clock=lambda: 1000)
        self.controller.update_statuses({key: ({"onOff": 1, "brightness": 65}, self.now)
                                         for key in (LEFT, RIGHT)})
        self.hue = PowerHue()
        self.strip = HueStripProxy(self.hue, "hue-monitor", clock=lambda: self.now,
                                   wall_clock=lambda: 1000)
        self.strip.process_once()
        self.identify = Identify(self.controller, {"hue-monitor": self.strip}, sleep=self.pause)
        self.identify.sender = lambda device, name, data: self.sent.append(
            ("blink", device["light"], name, data["value"])) or True
        self.controller.sender = self.identify.wrap(self.controller.sender)
        self.pauses = 0

    def pause(self, seconds):
        self.assertEqual(seconds, 0.3)
        self.pauses += 1
        # The scene writer keeps rendering during the blink.
        self.controller.render()


class IdentifyTests(IdentifyFixture):
    def test_govee_dips_twice_and_restores_without_racing_the_scene_writer(self):
        self.controller.submit(command(transitiontime=0))
        self.controller.submit(command(light="govee-right", transitiontime=0))
        self.controller.render()
        self.sent.clear()
        self.assertTrue(self.identify("govee-left"))
        blink = [entry[3] for entry in self.sent if entry[0] == "blink"]
        self.assertEqual(blink, [10, 65, 10, 65])
        self.assertEqual(self.pauses, 3)
        self.assertFalse([entry for entry in self.sent
                          if entry[0] == "scene" and entry[1] == "govee-left"])
        # After the blink the scene writer resends its own target.
        self.sent.clear()
        self.controller.render()
        self.assertTrue([entry for entry in self.sent if entry[1] == "govee-left"])

    def test_a_blink_waits_for_an_in_flight_controller_write(self):
        entered, release, order = threading.Event(), threading.Event(), []

        def slow(device, name, data):
            entered.set()
            release.wait(2)
            order.append("scene")
            return True

        send = self.identify.wrap(slow)
        writer = threading.Thread(target=send, args=(targets()[0], "brightness", {"value": 50}))
        writer.start()
        self.assertTrue(entered.wait(2))
        self.identify.sender = lambda device, name, data: order.append("blink") or True
        blinker = threading.Thread(target=self.identify, args=("govee-left",))
        blinker.start()
        blinker.join(0.2)
        self.assertTrue(blinker.is_alive(), "the blink waits for the write in flight")
        self.assertEqual(order, [])
        release.set()
        writer.join(2)
        blinker.join(2)
        self.assertEqual(order[0], "scene")
        self.assertEqual(set(order[1:]), {"blink"})

    def test_dim_lamps_dip_below_their_brightness(self):
        self.controller.update_statuses({LEFT: ({"onOff": 1, "brightness": 6}, self.now)})
        self.identify("govee-left")
        self.assertEqual([entry[3] for entry in self.sent], [2, 6, 2, 6])

    def test_govee_refusals(self):
        self.controller.update_statuses({LEFT: ({"onOff": 0, "brightness": 65}, self.now)})
        with self.assertRaises(IdentifyRefused) as refused:
            self.identify("govee-left")
        self.assertEqual(refused.exception.reason, "lamp off")
        self.controller.update_statuses({LEFT: ({"onOff": 1, "brightness": 65}, self.now)})
        self.now += 1
        with self.assertRaises(IdentifyRefused):
            self.identify("govee-left")
        self.controller.update_statuses({LEFT: ({"onOff": 1, "brightness": 65}, self.now)})
        gate = PowerOwner(targets(), clock=lambda: self.now, wall_clock=lambda: 1000)
        self.identify.power_owner = gate
        with self.assertRaises(IdentifyRefused) as refused:
            self.identify("govee-left")
        self.assertEqual(refused.exception.reason, "writer closed")
        self.assertEqual(self.sent, [])
        with self.assertRaises(ValueError):
            self.identify("left")

    def sync_video(self, composed=lambda role: role != "govee-right"):
        sync = GoveeController(targets(), sender=lambda device, name, data: True,
                               clock=lambda: self.now, wall_clock=lambda: 1000)
        sync.effective_target = lambda side: {"segments": [[9, 9, 9]] * 7, "brightness": 35}
        sync.razer["govee-left"] = sync.razer_frame["govee-left"] = self.now

        class Video:
            controller = sync

            def active(self):
                return True

        class Layout:
            def enabled(self, role, fallback=True):
                return composed(role)

        self.identify.video, self.identify.layout_store = Video(), Layout()
        return sync

    def test_sync_refuses_composed_native_roles_and_spares_the_others(self):
        self.sync_video()
        with self.assertRaises(IdentifyRefused) as refused:
            self.identify("hue-monitor")
        self.assertEqual(refused.exception.reason, "sync active")
        self.assertTrue(self.identify("govee-right"))

    def test_a_hue_light_for_scenes_only_blinks_through_its_bridge_during_sync(self):
        self.sync_video()
        # The area holds play-left only; hue-monitor is outside it, for scenes only (#78).
        self.identify.video.all_native = lambda: frozenset({"play-left"})
        calls = []
        self.identify._native = lambda role: calls.append(role) or True
        self.assertTrue(self.identify("hue-monitor"))
        self.assertEqual(calls, ["hue-monitor"])
        with self.assertRaises(IdentifyRefused) as refused:
            self.identify("play-left")
        self.assertEqual(refused.exception.reason, "sync active")

    def test_a_syncing_govee_blinks_with_dreamview_frames(self):
        sync = self.sync_video()
        sync.last_sent["govee-left"] = {"segments": [[9, 9, 9]] * 7}
        frames = []
        self.identify.sender = lambda device, name, data: frames.append(
            (device["light"], name, razer_colors(data))) or True
        self.assertTrue(self.identify("govee-left"))
        self.assertEqual(frames, [("govee-left", "razer", [[255, 255, 255]] * 7),
                                  ("govee-left", "razer", [[0, 0, 0]] * 7)] * 2)
        self.assertNotIn("govee-left", sync.last_sent, "the next Sync frame restores the picture")

    def test_a_syncing_govee_without_recent_frames_cannot_blink(self):
        sync = self.sync_video()
        sync.razer_frame["govee-left"] = self.now - 3
        with self.assertRaises(IdentifyRefused) as refused:
            self.identify("govee-left")
        self.assertEqual(refused.exception.reason, "sync active")

    def test_sync_frames_to_the_blinking_side_are_held(self):
        sync = self.sync_video()
        held = []
        sync.sender = self.identify.wrap(lambda device, name, data: held.append(
            device["light"]) or True)

        def pause(seconds):
            held.append(sync.sender(sync.by_light["govee-left"], "razer", {"pt": ""}))
            held.append(sync.sender(sync.by_light["govee-right"], "razer", {"pt": ""}))

        self.identify.sleep = pause
        self.identify.sender = lambda device, name, data: True
        self.identify("govee-left")
        self.assertEqual(held, [False, "govee-right", True] * 4)

    def native_relay(self, streams=True):
        class Relay:
            def __init__(self):
                self.sequence, self.overrides, self.shown = 0, {}, []

            def streams(self, role):
                return streams

            def override(self, role, rgb):
                if rgb is None:
                    self.overrides.pop(role, None)
                else:
                    self.overrides[role] = rgb

        relay = Relay()
        self.sync_video(composed=lambda role: True)
        self.identify.video.relay = relay

        def pause(seconds):
            relay.shown.append(relay.overrides.get("hue-monitor"))
            relay.sequence += 1

        self.identify.sleep = pause
        return relay

    def test_a_syncing_hue_light_blinks_on_its_stream_channels(self):
        relay = self.native_relay()
        self.assertTrue(self.identify("hue-monitor"))
        white = (65535, 65535, 65535)
        self.assertEqual(relay.shown, [white, (0, 0, 0)] * 2)
        self.assertEqual(relay.overrides, {}, "the Sync colors come back")
        self.assertEqual(self.hue.identified, 0, "no identify write while streaming")

    def test_a_syncing_hue_light_without_frames_cannot_blink(self):
        self.native_relay(streams=False)
        with self.assertRaises(IdentifyRefused) as refused:
            self.identify("hue-monitor")
        self.assertEqual(refused.exception.reason, "sync active")

    def test_a_hue_blink_with_no_frame_sent_fails(self):
        relay = self.native_relay()
        self.identify.sleep = lambda seconds: None
        with self.assertRaises(IdentifyFailed):
            self.identify("hue-monitor")
        self.assertEqual(relay.overrides, {})

    def test_blinks_in_a_row_keep_dreamview_fresh(self):
        self.sync_video()

        def pause(seconds):
            self.now += seconds

        self.identify.sleep = pause
        self.identify.sender = lambda device, name, data: True
        for _ in range(4):
            self.assertTrue(self.identify("govee-left"), "no Sync frame lands between blinks")

    def test_failed_dip_still_restores_and_reports_failure(self):
        sent = []
        self.identify.sender = lambda device, name, data: sent.append(data["value"]) or len(
            sent) != 1
        with self.assertRaises(IdentifyFailed):
            self.identify("govee-left")
        self.assertEqual(sent[-1], 65)

    def test_native_identify_only_while_on(self):
        self.assertTrue(self.identify("hue-monitor"))
        self.assertEqual(self.hue.identified, 1)
        self.hue.native["on"]["on"] = False
        self.strip.process_once()
        with self.assertRaises(IdentifyRefused):
            self.identify("hue-monitor")
        with self.assertRaises(IdentifyRefused) as refused:
            self.identify("play-left")
        self.assertEqual(refused.exception.reason, "unavailable")
        self.assertEqual(self.hue.identified, 1)

    def test_pinned_client_identify_is_a_separate_single_field_write(self):
        requests = []
        selected = PLAY_IDS["play-left"]

        def request(method, path, body):
            requests.append((method, path, body))
            return {"data": [{"rid": selected}], "errors": []}

        client = PinnedHueClient({"bridge": "192.168.10.21", "hue_key": "test"},
                                 requester=request, resource_id=selected,
                                 profile=NativeHueProfile.from_dict(profile_data()))
        client.identify()
        self.assertEqual(requests, [("PUT", "/clip/v2/resource/light/" + selected,
                                     {"identify": {"action": "identify"}})])
        with self.assertRaises(ValueError):
            client.write({"identify": {"action": "identify"}})
        with self.assertRaises(ValueError):
            client.write({"on": {"on": True}})

        def rejected(method, path, body):
            return {"data": [], "errors": [{"description": "not allowed"}]}

        client.requester = rejected
        with self.assertRaises(ValueError):
            client.identify()


class IdentifyRouteTests(IdentifyFixture):
    def serve(self, **kwargs):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(
            self.controller, "adapter-key", hue_proxies={"hue-monitor": self.strip}, **kwargs))
        worker = threading.Thread(target=server.serve_forever)
        worker.start()

        def close():
            server.shutdown()
            server.server_close()
            worker.join(2)

        self.addCleanup(close)
        return server.server_port

    def post(self, port, body, token="adapter-key", path="/identify"):
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json", "X-Adapter-Token": token})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as response:
            with response:
                return response.code, json.load(response)

    def test_identify_route(self):
        self.identify.sleep = lambda seconds: None
        port = self.serve(identify=self.identify)
        self.assertEqual(self.post(port, {"role": "govee-left"}), (200, {"accepted": True}))
        self.assertEqual(self.post(port, {"role": "hue-monitor"}), (200, {"accepted": True}))
        self.assertEqual(self.hue.identified, 1)
        self.controller.update_statuses({RIGHT: ({"onOff": 0}, self.now)})
        self.assertEqual(self.post(port, {"role": "govee-right"}),
                         (409, {"accepted": False, "reason": "lamp off"}))
        self.hue.identify = lambda: (_ for _ in ()).throw(OSError("bridge unreachable"))
        self.assertEqual(self.post(port, {"role": "hue-monitor"}), (503, {"accepted": False}))
        for body in ({"role": "left"}, {"role": 1}, {}, {"role": "govee-left", "x": 1}, []):
            with self.subTest(body=body):
                self.assertEqual(self.post(port, body)[0], 400)
        sent = len(self.sent)
        self.assertEqual(self.post(port, {"light": "govee-left"}, token="wrong")[0], 403)
        self.assertEqual(len(self.sent), sent)

    def test_identify_route_is_absent_without_identify(self):
        port = self.serve()
        self.assertEqual(self.post(port, {"role": "govee-left"})[0], 404)

    def test_native_power_route_marks_an_owner_change(self):
        marked = []

        class Session:
            def owner_power(self, role):
                marked.append(role)

            def snapshot(self):
                return {"running": True, "roles": ["hue-monitor"], "last_result": "session"}

        self.strip.set_power = lambda on: True
        port = self.serve(sync_off=Session())
        self.assertEqual(self.post(port, {"lights": {"hue-monitor": True}}, path="/native-power"),
                         (200, {"accepted": True}))
        self.assertEqual(marked, ["hue-monitor"])


spec = importlib.util.spec_from_file_location(
    "setup_studio_identify",
    Path(__file__).resolve().parents[1] / "studio/server.py")
studio = importlib.util.module_from_spec(spec)
spec.loader.exec_module(studio)


class StudioIdentifyTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory(prefix="tmp_studio_identify_")
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.calls = []
        self.answer = (200, {"accepted": True})

        def post(role):
            self.calls.append(role)
            if isinstance(self.answer, Exception):
                raise self.answer
            return self.answer

        server = ThreadingHTTPServer(("127.0.0.1", 0), studio.handler_for(
            self.folder / "missing-token", self.folder,
            identify_post=post))
        worker = threading.Thread(target=server.serve_forever)
        worker.start()
        self.base = "http://127.0.0.1:" + str(server.server_port)

        def close():
            server.shutdown()
            server.server_close()
            worker.join(2)

        self.addCleanup(close)

    def post(self, body, **headers):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        request = urllib.request.Request(
            self.base + "/api/identify", data=data, method="POST",
            headers={"Content-Type": "application/json", "Origin": self.base, **headers})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=5) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as response:
            with response:
                return response.code, json.load(response)

    def test_role_lamps_map_to_adapter_roles(self):
        for lamp, role in LAMPS.items():
            with self.subTest(lamp=lamp):
                self.assertEqual(self.post({"lamp": lamp}), (200, {"accepted": True}))
                self.assertEqual(self.calls[-1], role)

    def test_an_enrolled_device_light_is_forwarded_by_its_registry_id(self):
        light = "device-" + "a" * 32
        self.assertEqual(self.post({"lamp": light}), (200, {"accepted": True}))
        self.assertEqual(self.calls[-1], light)

    def test_validation_and_protections(self):
        for lamp in ("device-" + "a" * 31, "virtual-lamp1", "left"):
            with self.subTest(lamp=lamp):
                self.assertEqual(self.post({"lamp": lamp})[0], 404)
        for body in ({}, {"lamp": 1}, {"lamp": "govee-left", "role": "govee-left"}, [], b"{"):
            with self.subTest(body=body):
                self.assertEqual(self.post(body)[0], 400)
        self.assertEqual(self.post({"lamp": "govee-left"}, Origin="http://evil.example")[0],
                         403)
        self.assertEqual(self.post({"lamp": "govee-left"},
                                   **{"Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(self.post({"lamp": "govee-left"},
                                   **{"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.calls, [])

    def test_adapter_answers_are_projected(self):
        self.answer = (409, {"accepted": False, "reason": "lamp off"})
        self.assertEqual(self.post({"lamp": "govee-left"}),
                         (409, {"accepted": False, "reason": "lamp off"}))
        self.answer = (409, {"accepted": False, "reason": "<secret detail>"})
        self.assertEqual(self.post({"lamp": "govee-left"}),
                         (409, {"accepted": False, "reason": "refused"}))
        self.answer = (503, {"accepted": False})
        self.assertEqual(self.post({"lamp": "govee-left"})[0], 503)
        self.answer = OSError("adapter down")
        self.assertEqual(self.post({"lamp": "govee-left"}),
                         (503, {"accepted": False, "reason": "adapter unavailable"}))

    def test_live_output_hold_refuses_without_calling_the_adapter(self):
        (self.folder / "live-output-hold").write_text("")
        self.assertEqual(self.post({"lamp": "hue-monitor"}),
                         (409, {"accepted": False, "reason": "paused"}))
        self.assertEqual(self.calls, [])



class StudioCalibrationTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory(prefix="tmp_studio_calibration_")
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.calls = []
        self.answer = (200, {"active": True})

        def post(route, body):
            self.calls.append((route, body))
            if isinstance(self.answer, Exception):
                raise self.answer
            return self.answer

        server = ThreadingHTTPServer(("127.0.0.1", 0), studio.handler_for(
            self.folder / "missing-token", self.folder,
            calibration_post=post))
        worker = threading.Thread(target=server.serve_forever)
        worker.start()
        self.base = "http://127.0.0.1:" + str(server.server_port)

        def close():
            server.shutdown()
            server.server_close()
            worker.join(2)

        self.addCleanup(close)

    def post(self, path, body, **headers):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        request = urllib.request.Request(
            self.base + path, data=data, method="POST",
            headers={"Content-Type": "application/json", "Origin": self.base, **headers})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=5) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as response:
            with response:
                return response.code, json.load(response)

    def test_requests_reach_the_adapter_and_answers_are_projected(self):
        start = {"lamp": "govee-left", "reference": "play-left", "level": "bright",
                 "calibration": {"warmth": 10, "tint": 0, "lowLight": 0}}
        self.assertEqual(self.post("/api/calibration", start), (200, {"active": True}))
        self.answer = (200, {"active": False})
        self.assertEqual(self.post("/api/calibration/end", {}), (200, {"active": False}))
        self.assertEqual(self.calls, [("/calibration", start), ("/calibration/end", {})])
        self.answer = (409, {"active": False, "reason": "sync active"})
        self.assertEqual(self.post("/api/calibration", start),
                         (409, {"active": False, "reason": "sync active"}))
        self.answer = (409, {"reason": "<detail>"})
        self.assertEqual(self.post("/api/calibration", start)[1]["reason"], "refused")
        self.answer = (400, {"error": "x"})
        self.assertEqual(self.post("/api/calibration", start)[0], 400)
        self.answer = OSError("adapter down")
        self.assertEqual(self.post("/api/calibration", start),
                         (503, {"active": False, "reason": "adapter unavailable"}))

    def test_protections(self):
        self.assertEqual(self.post("/api/calibration", [])[0], 400)
        self.assertEqual(self.post("/api/calibration", b"{")[0], 400)
        self.assertEqual(self.post("/api/calibration", {}, Origin="http://evil.example")[0], 403)
        (self.folder / "live-output-hold").write_text("")
        self.assertEqual(self.post("/api/calibration/end", {}),
                         (409, {"active": False, "reason": "paused"}))
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
