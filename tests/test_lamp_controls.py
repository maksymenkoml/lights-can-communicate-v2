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

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "experiments/hue-bridge"))
from desk_layout import LayoutStore, composed, validate_layout  # noqa: E402
from entertainment_stream import VideoLease  # noqa: E402
from homekit_power import HomeKitPower  # noqa: E402
from hue_strip_proxy import HueStripProxy, PinnedHueClient  # noqa: E402
from identify import Identify, IdentifyFailed, IdentifyRefused  # noqa: E402
from live_adapter import PilotController, handler_for  # noqa: E402
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

LAMPS = {"govee-left": "left", "govee-right": "right", "hue-monitor": "strip",
         "play-left": "hue_left", "play-center": "hue_center", "play-right": "hue_right"}


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
                self.assertEqual(result["version"], 5)
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
            self.assertFalse(store.enabled("left"))
            self.assertTrue(store.present("left"))
            self.assertFalse(store.enabled("strip"))
            self.assertTrue(store.present("strip"))
            profile = store.sync_profile()
            self.assertEqual(profile["enabled_roles"], ["right", "hue_right"])
            self.assertEqual(profile["native_roles"], ["hue_right"])
            # A removed row is never switched, even when it says "off".
            self.assertEqual(store.sync_off_roles(), ["left", "strip"])


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
        self.strip = HueStripProxy(self.hue, clock=self.lamps.clock, wall_clock=lambda: 1000)
        self.strip.process_once()
        self.active = False

    def homekit(self):
        power = HomeKitPower(targets(), self.folder / "homekit-power.json", self.lamps.send,
                             self.lamps.observe, clock=self.lamps.clock, sleep=self.lamps.sleep)
        for side in ("left", "right"):
            self.assertTrue(power.set({"role": side, "on": True}))
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
                            "states": states})

    def session(self, govee):
        return SyncOffSession(self.store, lambda: self.active, self.state, govee=govee,
                              native={"strip": self.strip}, observe=self.lamps.observe,
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
        self.assertEqual(self.turns("left"), [])
        self.active = True
        session.step()
        self.assertEqual(self.turns("left"), [0])
        self.assertEqual(self.turns("right"), [])
        self.assertFalse(self.lamps.native["left"])
        self.assertFalse(power.allowed("left"))
        self.assertTrue(power.allowed("right"))
        self.assertEqual(self.hue.power_calls, [False])
        # HomeKit keeps showing the owner's intent, and tick() never adopts the hold.
        for _ in range(40):
            self.lamps.now += 0.5
            power.tick()
            session.step()
        roles = power.state()["roles"]
        self.assertIs(roles["left"]["on"], True)
        self.assertIs(roles["left"]["requested_on"], True)
        self.assertIs(roles["left"]["native_on"], False)
        self.assertTrue(power.snapshot()["roles"]["left"]["sync_hold"])
        self.assertTrue(power.snapshot()["roles"]["left"]["requested_on"])
        saved = json.loads(self.state.read_text())
        self.assertEqual(saved, {"version": 1, "roles": ["left", "strip"]})
        self.assertEqual(stat.S_IMODE(os.stat(self.state).st_mode), 0o600)
        self.active = False
        session.step()
        self.assertEqual(self.turns("left"), [0, 1])
        self.assertTrue(self.lamps.native["left"])
        self.assertTrue(power.allowed("left"))
        self.assertEqual(self.hue.power_calls, [False, True])
        self.assertFalse(self.state.exists())
        self.assertEqual(session.snapshot()["roles"], [])

    def test_lamps_already_off_stay_off(self):
        power = self.homekit()
        self.assertTrue(power.set({"role": "left", "on": False}))
        self.hue.native["on"]["on"] = False
        self.strip.process_once()
        session = self.session(power)
        self.lamps.sent.clear()
        self.active = True
        session.step()
        self.assertFalse(power.sync_held("left"))
        self.assertFalse(self.state.exists())
        self.active = False
        session.step()
        self.assertEqual(self.turns("left"), [])
        self.assertEqual(self.hue.power_calls, [])
        self.assertFalse(self.lamps.native["left"])

    def test_owner_changes_during_the_session_win(self):
        power = self.homekit()
        session = self.session(power)
        self.active = True
        session.step()
        # HomeKit Off during Sync: intent becomes Off and nothing is restored.
        self.assertTrue(power.set({"role": "left", "on": False}))
        self.assertFalse(power.sync_held("left"))
        # Hue app On through the virtual bridge's native power route.
        session.owner_power("strip")
        self.assertEqual(json.loads(self.state.read_text())["roles"], ["left"])
        # The watcher drops the Govee record once the authority dropped its hold.
        session.step()
        self.assertFalse(self.state.exists())
        self.lamps.sent.clear()
        self.active = False
        session.step()
        self.assertEqual(self.turns("left"), [])
        self.assertFalse(self.lamps.native["left"])
        self.assertEqual(self.hue.power_calls, [False])

    def test_owner_on_during_the_session_turns_the_lamp_on_and_keeps_it(self):
        power = self.homekit()
        session = self.session(power)
        self.active = True
        session.step()
        self.assertTrue(power.set({"role": "left", "on": True}))
        self.assertTrue(self.lamps.native["left"])
        self.assertTrue(power.allowed("left"))
        # The physical Hue app switched the strip on directly at the bridge.
        self.hue.native["on"]["on"] = True
        self.strip.process_once()
        session.step()
        self.assertEqual(session.snapshot()["roles"], [])
        self.active = False
        self.lamps.sent.clear()
        session.step()
        self.assertEqual(self.turns("left"), [])
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
        self.assertEqual(self.turns("left"), [0])
        self.assertEqual(self.hue.power_calls, [False])
        self.assertEqual(session.snapshot()["roles"], ["left", "strip"])
        # The owner switches the left lamp on again: it is not switched off again.
        self.assertTrue(power.set({"role": "left", "on": True}))
        for _ in range(5):
            self.lamps.now += 0.5
            power.tick()
            session.step()
        self.assertEqual(self.turns("left"), [0, 1])
        self.assertEqual(session.snapshot()["roles"], ["strip"])
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
        self.assertEqual(self.turns("left"), [0, 1])
        # Session end restores what is still recorded, and only that.
        self.active = False
        session.step()
        self.assertEqual(self.hue.power_calls, [False, True, False, True])
        self.assertEqual(self.turns("left"), [0, 1])

    def test_failed_restore_of_a_role_leaving_off_is_retried_during_the_session(self):
        session = self.session(None)
        self.active = True
        session.step()
        self.hue.fail_power = True
        self.store.save(with_roles(**{"govee-left": "off"}))
        session.step()
        self.assertEqual(session.snapshot()["roles"], ["strip"])
        self.hue.fail_power = False
        session.step()
        self.assertEqual(session.snapshot()["roles"], ["strip"], "retry waits a second")
        self.lamps.now += 1.5
        session.step()
        self.assertEqual(session.snapshot()["roles"], [])
        self.assertTrue(self.hue.native["on"]["on"])

    def test_power_owner_hold_closes_the_gate_and_owner_off_wins(self):
        gate = self.owner()
        session = self.session(gate)
        self.active = True
        session.step()
        self.assertEqual(self.turns("left"), [0])
        self.assertFalse(gate.allowed("left"))
        self.assertTrue(gate.allowed("right"))
        # Owner heartbeats that keep the same intent keep the hold.
        self.assertTrue(self.grant(gate, left=True, right=True))
        self.assertFalse(gate.allowed("left"))
        self.assertTrue(gate.snapshot()["roles"]["left"]["requested_on"])
        self.active = False
        session.step()
        self.assertEqual(self.turns("left"), [0, 1])
        self.assertTrue(gate.allowed("left"))
        # Second session: the owner switches the lamp off in HomeKit during Sync.
        self.active = True
        session.step()
        self.assertTrue(self.grant(gate, left=False, right=True))
        self.assertFalse(gate.sync_held("left"))
        session.step()
        self.assertNotIn("left", session.snapshot()["roles"])
        self.active = False
        session.step()
        self.assertEqual(self.turns("left"), [0, 1, 0])
        self.assertFalse(self.lamps.native["left"])

    def test_without_a_power_authority_govee_is_untouched(self):
        session = self.session(None)
        self.active = True
        session.step()
        self.assertEqual(self.lamps.sent, [])
        self.assertEqual(session.snapshot()["roles"], ["strip"])
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
        self.assertFalse(power.sync_held("left"))
        self.assertTrue(self.lamps.native["left"])
        self.assertTrue(power.sync_held("right"))
        self.assertFalse(self.lamps.native["right"])
        self.assertEqual(self.hue.power_calls, [False, True])
        self.active = False
        session.step()
        self.assertFalse(power.sync_held("right"))
        self.assertTrue(self.lamps.native["right"])
        self.assertEqual(self.hue.power_calls, [False, True])

    def test_a_lamp_is_left_alone_when_its_record_cannot_be_saved(self):
        power = self.homekit()
        self.state = self.folder / "missing" / STATE_FILE
        session = self.session(power)
        self.lamps.sent.clear()
        self.active = True
        session.step()
        self.assertEqual(self.turns("left"), [], "no Off without a recovery record")
        self.assertEqual(self.hue.power_calls, [])
        self.assertTrue(power.allowed("left"))
        self.assertEqual(session.snapshot()["roles"], [])

    def test_failed_govee_restore_stays_held_recorded_and_is_retried(self):
        for gate in (self.homekit(), self.owner()):
            with self.subTest(gate=type(gate).__name__):
                self.lamps.native["left"] = True
                session = self.session(gate)
                self.active = True
                session.step()
                self.assertFalse(self.lamps.native["left"])
                self.lamps.ignore.add("left")
                self.active = False
                session.step()
                self.assertIn("left", json.loads(self.state.read_text())["roles"])
                self.assertFalse(gate.allowed("left"), "the hold stays until the lamp is back")
                self.lamps.ignore.discard("left")
                self.lamps.now += 2
                session.step()
                self.assertTrue(self.lamps.native["left"])
                self.assertNotIn("left", gate.holds, "a confirmed restore ends the hold")
                self.assertNotIn("left", session.snapshot()["roles"])
                session.close()

    def test_failed_native_restore_is_retried_and_kept_on_disk(self):
        session = self.session(None)
        self.active = True
        session.step()
        self.hue.fail_power = True
        self.active = False
        session.step()
        self.assertEqual(json.loads(self.state.read_text())["roles"], ["strip"])
        self.hue.fail_power = False
        self.lamps.now += 2
        session.step()
        self.assertFalse(self.state.exists())
        self.assertTrue(self.hue.native["on"]["on"])

    def test_crash_file_is_restored_on_start_and_deleted(self):
        power = self.homekit()
        self.lamps.native["left"] = False
        self.hue.native["on"]["on"] = False
        self.strip.process_once()
        self.state.write_text(json.dumps({"version": 1, "roles": ["left", "strip"]}))
        restarted = HomeKitPower(targets(), self.folder / "homekit-power.json", self.lamps.send,
                                 self.lamps.observe, clock=self.lamps.clock,
                                 sleep=self.lamps.sleep)
        self.assertIsNot(restarted, power)
        session = self.session(restarted)
        # The crash-time Off is held, so tick() cannot adopt it before the restore.
        self.assertTrue(restarted.sync_held("left"))
        for _ in range(20):
            self.lamps.now += 0.5
            restarted.tick()
        self.assertTrue(restarted.state()["roles"]["left"]["requested_on"])
        session.step()
        self.assertTrue(self.lamps.native["left"])
        self.assertTrue(restarted.allowed("left"))
        self.assertTrue(self.hue.native["on"]["on"])
        self.assertFalse(self.state.exists())

    def test_crash_restore_waits_for_a_native_reading(self):
        power = self.homekit()
        self.lamps.native["left"] = False
        self.lamps.reachable["left"] = False
        self.state.write_text(json.dumps({"version": 1, "roles": ["left"]}))
        session = self.session(power)
        self.lamps.sent.clear()
        session.step()
        self.assertEqual(self.turns("left"), [])
        self.assertTrue(self.state.exists())
        self.lamps.reachable["left"] = True
        self.lamps.now += 2
        session.step()
        self.assertEqual(self.turns("left"), [1])
        self.assertFalse(self.state.exists())

    def test_shutdown_restores_immediately(self):
        power = self.homekit()
        session = self.session(power)
        self.active = True
        session.step()
        session.close()
        self.assertTrue(self.lamps.native["left"])
        self.assertTrue(self.hue.native["on"]["on"])
        self.assertFalse(self.state.exists())
        session.step()
        self.assertEqual(self.turns("left"), [1, 0, 1])


class FenceTests(unittest.TestCase):
    def test_an_off_native_role_does_not_trip_the_sync_safety_fence(self):
        with TemporaryDirectory(prefix="tmp_sync_off_fence_") as folder:
            store = LayoutStore(Path(folder) / "desk-layout.json")
            store.save(with_roles(**{"hue-monitor": "off"}))
            now = [10]
            controller = PilotController(targets(), sender=lambda *args: True,
                                         clock=lambda: now[0], wall_clock=lambda: 1000,
                                         layout_store=store)
            proxies = []
            for role in ("strip", "hue_left", "hue_center", "hue_right"):
                proxy = FakeProxy()
                proxy.role = role
                proxies.append(proxy)
            # Sync switched the strip off: fresh, healthy and native Off.
            proxies[0].value["native_on"] = False

            class Relay:
                frames = []

                def start(self, profile=None):
                    return True

                def send(self, frame):
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
        self.controller = PilotController(
            targets(), sender=lambda device, name, data: self.sent.append(
                ("scene", device["side"], name, data["value"] if name == "brightness" else None))
            or True, clock=lambda: self.now, wall_clock=lambda: 1000)
        self.controller.update_statuses({key: ({"onOff": 1, "brightness": 65}, self.now)
                                         for key in (LEFT, RIGHT)})
        self.hue = PowerHue()
        self.strip = HueStripProxy(self.hue, clock=lambda: self.now, wall_clock=lambda: 1000)
        self.strip.process_once()
        self.identify = Identify(self.controller, {"strip": self.strip}, sleep=self.pause)
        self.identify.sender = lambda device, name, data: self.sent.append(
            ("blink", device["side"], name, data["value"])) or True
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
        self.controller.submit(command(side="right", transitiontime=0))
        self.controller.render()
        self.sent.clear()
        self.assertTrue(self.identify("left"))
        blink = [entry[3] for entry in self.sent if entry[0] == "blink"]
        self.assertEqual(blink, [10, 65, 10, 65])
        self.assertEqual(self.pauses, 3)
        self.assertFalse([entry for entry in self.sent
                          if entry[0] == "scene" and entry[1] == "left"])
        # After the blink the scene writer resends its own target.
        self.sent.clear()
        self.controller.render()
        self.assertTrue([entry for entry in self.sent if entry[1] == "left"])

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
        blinker = threading.Thread(target=self.identify, args=("left",))
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
        self.identify("left")
        self.assertEqual([entry[3] for entry in self.sent], [2, 6, 2, 6])

    def test_govee_refusals(self):
        self.controller.update_statuses({LEFT: ({"onOff": 0, "brightness": 65}, self.now)})
        with self.assertRaises(IdentifyRefused) as refused:
            self.identify("left")
        self.assertEqual(refused.exception.reason, "lamp off")
        self.controller.update_statuses({LEFT: ({"onOff": 1, "brightness": 65}, self.now)})
        self.now += 1
        with self.assertRaises(IdentifyRefused):
            self.identify("left")
        self.controller.update_statuses({LEFT: ({"onOff": 1, "brightness": 65}, self.now)})
        gate = PowerOwner(targets(), clock=lambda: self.now, wall_clock=lambda: 1000)
        self.identify.power_owner = gate
        with self.assertRaises(IdentifyRefused) as refused:
            self.identify("left")
        self.assertEqual(refused.exception.reason, "writer closed")
        self.assertEqual(self.sent, [])
        with self.assertRaises(ValueError):
            self.identify("govee-left")

    def test_sync_active_refuses_composed_roles_only(self):
        class Video:
            def active(self):
                return True

        class Layout:
            def enabled(self, role, fallback=True):
                return role != "right"

        self.identify.video, self.identify.layout_store = Video(), Layout()
        for role in ("left", "strip"):
            with self.subTest(role=role), self.assertRaises(IdentifyRefused) as refused:
                self.identify(role)
            self.assertEqual(refused.exception.reason, "sync active")
        self.assertTrue(self.identify("right"))

    def test_failed_dip_still_restores_and_reports_failure(self):
        sent = []
        self.identify.sender = lambda device, name, data: sent.append(data["value"]) or len(
            sent) != 1
        with self.assertRaises(IdentifyFailed):
            self.identify("left")
        self.assertEqual(sent[-1], 65)

    def test_native_identify_only_while_on(self):
        self.assertTrue(self.identify("strip"))
        self.assertEqual(self.hue.identified, 1)
        self.hue.native["on"]["on"] = False
        self.strip.process_once()
        with self.assertRaises(IdentifyRefused):
            self.identify("strip")
        with self.assertRaises(IdentifyRefused) as refused:
            self.identify("hue_left")
        self.assertEqual(refused.exception.reason, "unavailable")
        self.assertEqual(self.hue.identified, 1)

    def test_pinned_client_identify_is_a_separate_single_field_write(self):
        requests = []
        selected = PLAY_IDS["hue_left"]

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
            self.controller, "adapter-key", hue_proxy=self.strip, **kwargs))
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
        self.assertEqual(self.post(port, {"role": "left"}), (200, {"accepted": True}))
        self.assertEqual(self.post(port, {"role": "strip"}), (200, {"accepted": True}))
        self.assertEqual(self.hue.identified, 1)
        self.controller.update_statuses({RIGHT: ({"onOff": 0}, self.now)})
        self.assertEqual(self.post(port, {"role": "right"}),
                         (409, {"accepted": False, "reason": "lamp off"}))
        self.hue.identify = lambda: (_ for _ in ()).throw(OSError("bridge unreachable"))
        self.assertEqual(self.post(port, {"role": "strip"}), (503, {"accepted": False}))
        for body in ({"role": "govee-left"}, {"role": 1}, {}, {"role": "left", "x": 1}, []):
            with self.subTest(body=body):
                self.assertEqual(self.post(port, body)[0], 400)
        sent = len(self.sent)
        self.assertEqual(self.post(port, {"role": "left"}, token="wrong")[0], 403)
        self.assertEqual(len(self.sent), sent)

    def test_identify_route_is_absent_without_identify(self):
        port = self.serve()
        self.assertEqual(self.post(port, {"role": "left"})[0], 404)

    def test_native_power_route_marks_an_owner_change(self):
        marked = []

        class Session:
            def owner_power(self, role):
                marked.append(role)

            def snapshot(self):
                return {"running": True, "roles": ["strip"], "last_result": "session"}

        self.strip.set_power = lambda on: True
        port = self.serve(sync_off=Session())
        self.assertEqual(self.post(port, {"roles": {"strip": True}}, path="/native-power"),
                         (200, {"accepted": True}))
        self.assertEqual(marked, ["strip"])


spec = importlib.util.spec_from_file_location(
    "setup_studio_identify",
    Path(__file__).resolve().parents[1] / "experiments/setup-studio/server.py")
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
            self.folder / "missing-token", self.folder / "desk-layout.json",
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

    def test_validation_and_protections(self):
        for lamp in ("device-" + "a" * 32, "virtual-lamp1", "left"):
            with self.subTest(lamp=lamp):
                self.assertEqual(self.post({"lamp": lamp})[0], 404)
        for body in ({}, {"lamp": 1}, {"lamp": "govee-left", "role": "left"}, [], b"{"):
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


if __name__ == "__main__":
    unittest.main()
