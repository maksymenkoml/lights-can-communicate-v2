"""Host-side live adapter: authenticated Hue transition targets -> pinned Govee lights."""

import argparse
import colorsys
import copy
import hmac
import json
import math
import os
import secrets
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

if Path(__file__).is_file():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from calibration_session import CalibrationRefused, CalibrationSession  # noqa: E402
from desk_layout import LayoutStore  # noqa: E402
from entertainment_stream import GRADIENT_MODE, NATIVE_SOURCES  # noqa: E402
from govee_calibration import GoveeCalibration  # noqa: E402
from govee_models import target_capabilities  # noqa: E402
from govee_segments import interpolate, razer_data, razer_mode, segment_data  # noqa: E402
from govee_transport import GoveeTransport  # noqa: E402
from homekit_power import (  # noqa: E402
    HomeKitPower,
    HomeKitRequests,
    SignatureVerifier,
    response_signature,
)
from hue_area import HueArea  # noqa: E402
from hue_entertainment_relay import HueEntertainmentRelay  # noqa: E402
from hue_strip_proxy import HueStripProxy, PinnedHueClient, xy_color  # noqa: E402
from identify import Identify, IdentifyFailed, IdentifyRefused  # noqa: E402
from legacy_wire import (  # noqa: E402
    owner_envelope,
    owner_exchange,
    owner_observed,
)
from light_enrollment import LightEnrollment, LightRefused  # noqa: E402
from power_owner import PowerOwner  # noqa: E402
from power_requests import PowerRequests, PowerRequestUnavailable, validate_capability  # noqa: E402
from stream_diagnostics import StreamDiagnostics  # noqa: E402
from sync_health import export as export_diagnostics  # noqa: E402
from sync_off import STATE_FILE as SYNC_OFF_FILE  # noqa: E402
from sync_off import SyncOffSession  # noqa: E402
from video_output import NoNativeRelay, VideoOutput  # noqa: E402

import lights_can_communicate_v2 as lan  # noqa: E402
from lights_can_communicate_v2_core.discovery import DeviceCatalog  # noqa: E402
from lights_can_communicate_v2_core.hue_discovery import HueDiscoveryReader  # noqa: E402
from lights_can_communicate_v2_core.light_registry import (  # noqa: E402
    LEGACY_SIDES,
    LightRegistry,
)
from lights_can_communicate_v2_core.live_preview import OutputPreviewStore, build_preview  # noqa: E402


def number(value, low, high):
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise ValueError("Expected a finite number")
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError("Numeric value is outside the accepted range")
    return value


def normalize_state(state, scale, segment_count=0):
    if not isinstance(state, dict) or not isinstance(state.get("on"), bool):
        raise ValueError("Expected a light state with boolean on")
    if not state["on"]:
        return {"on": False}
    brightness = number(state.get("bri", 254), 0, 254)
    result = {
        "on": True,
        "brightness": max(1, round(brightness / 254 * 100 * lan.parse_brightness_scale(scale))),
    }
    mode = state.get("colormode", "xy")
    if "segments" in state or state.get("_color_kind") == "gradient":
        if segment_count != 7:
            raise ValueError("Zoned output requires an explicitly enabled H6076 profile")
        if "segments" in state:
            stops = state["segments"]
        else:
            gradient = state.get("gradient")
            if not isinstance(gradient, dict):
                raise ValueError("Expected a gradient object")
            points = gradient.get("points")
            if not isinstance(points, list) or not 1 <= len(points) <= 5:
                raise ValueError("Expected a bounded gradient palette")
            stops = []
            for point in points:
                if not isinstance(point, dict) or not isinstance(point.get("color"), dict):
                    raise ValueError("Expected a gradient color point")
                color = xy_color(point["color"].get("xy"))
                stops.append(lan.xy_to_rgb(color["x"], color["y"]))
        result["segments"] = interpolate(stops)
    elif mode == "rgb":
        rgb = state.get("rgb")
        if not isinstance(rgb, list) or len(rgb) != 3:
            raise ValueError("Expected RGB triple")
        result["rgb"] = tuple(round(number(value, 0, 255)) for value in rgb)
    elif mode == "ct":
        mirek = number(state.get("ct"), 100, 1000)
        result["kelvin"] = max(2000, min(9000, round(1_000_000 / mirek)))
    elif mode == "hs":
        hue = number(state.get("hue"), 0, 65535)
        saturation = number(state.get("sat"), 0, 254)
        result["rgb"] = tuple(
            round(value * 255) for value in colorsys.hsv_to_rgb(hue / 65535, saturation / 254, 1)
        )
    else:
        xy = state.get("xy")
        if not isinstance(xy, list) or len(xy) != 2:
            raise ValueError("Expected xy color")
        result["rgb"] = lan.xy_to_rgb(number(xy[0], 0, 1), number(xy[1], 0.0001, 1))
    return result


def as_rgb(target):
    if "segments" in target:
        return target["segments"][3]
    if "rgb" in target:
        return target["rgb"]
    # Approximate CT only while crossing color modes. Final CT uses the white channel.
    # Curve coefficients: https://tannerhelland.com/2012/09/18/convert-temperature-rgb-algorithm-code.html
    temperature = target["kelvin"] / 100
    red = 255 if temperature <= 66 else 329.698727446 * (temperature - 60) ** -0.1332047592
    green = (
        99.4708025861 * math.log(temperature) - 161.1195681661
        if temperature <= 66
        else 288.1221695283 * (temperature - 60) ** -0.0755148492
    )
    blue = (
        255 if temperature >= 66 else 138.5177312231 * math.log(temperature - 10) - 305.0447927307
    )
    return tuple(max(0, min(255, round(value))) for value in (red, green, blue))


def blend(first, last, fraction):
    if fraction >= 1:
        return dict(last)
    fraction = max(0, fraction)
    result = {
        "on": True,
        "brightness": round(
            first["brightness"] + (last["brightness"] - first["brightness"]) * fraction
        ),
    }
    if "segments" in first or "segments" in last:
        start = first.get("segments") or [as_rgb(first)] * 7
        end = last.get("segments") or [as_rgb(last)] * 7
        result["segments"] = [
            tuple(round(a + (b - a) * fraction) for a, b in zip(left, right))
            for left, right in zip(start, end)
        ]
    elif "kelvin" in first and "kelvin" in last:
        result["kelvin"] = round(first["kelvin"] + (last["kelvin"] - first["kelvin"]) * fraction)
    else:
        result["rgb"] = tuple(
            round(start + (end - start) * fraction)
            for start, end in zip(as_rgb(first), as_rgb(last))
        )
    return result


def native_target(status, fallback):
    if not status or "brightness" not in status:
        return dict(fallback)
    result = {"on": True, "brightness": max(1, min(100, status["brightness"]))}
    if status.get("colorTemInKelvin", 0):
        result["kelvin"] = max(2000, min(9000, status["colorTemInKelvin"]))
    else:
        result["rgb"] = tuple(status.get("color", {}).get(key, 0) for key in ("r", "g", "b"))
    return result


# Defaults for a light without a capability-table entry (an explicit seven-segment
# profile): DreamView frames per second while Sync streams, and the DreamView timing.
# The tested values per model are in govee_models.MODELS and take precedence.
STREAM_FRAMES_PER_SECOND = 30
RAZER_IDLE_SECONDS = 45
RAZER_LOST_SECONDS = 5


class GoveeController:
    def __init__(
        self,
        targets,
        sender=lan.send_to_target,
        clock=time.monotonic,
        wall_clock=time.time,
        layout_store=None,
        power_owner=None,
        configuration="sync",
    ):
        if configuration not in ("sync", "scene"):
            raise ValueError("Unknown output configuration")
        # "sync" is the Sync composition; "scene" is the output without Sync, which
        # uses scene membership, the scene correction and the scene fade.
        self.configuration = configuration
        self.layout_store = layout_store
        self.power_owner = power_owner
        self.observer_epoch = secrets.token_hex(8)
        self.power_revisions = {}
        self.targets = copy.deepcopy(targets)
        if any(type(item.get("light")) is not str for item in self.targets):
            raise ValueError("Every Govee target needs its light id")
        self.by_light = {item["light"]: item for item in self.targets}
        if len(self.by_light) != len(self.targets):
            raise ValueError("Govee light ids must be distinct")
        # Per-light model capabilities: DreamView timing and the default stream rate.
        self.caps = {item["light"]: target_capabilities(item) for item in self.targets}
        self.packet_rates = {
            item["light"]: number(item["max_commands_per_second"], 1, 100)
            if "max_commands_per_second" in item
            else None
            for item in self.targets
        }
        self.gradient_packet_rates = {
            item["light"]: number(item["max_gradient_packets_per_second"], 1, 100)
            if "max_gradient_packets_per_second" in item
            else None
            for item in self.targets
        }
        self.stream_rates = {
            item["light"]: number(item["max_stream_frames_per_second"], 1, 60)
            if "max_stream_frames_per_second" in item
            else self.caps[item["light"]].get("stream_frames_per_second",
                                              STREAM_FRAMES_PER_SECOND)
            for item in self.targets
        }
        # Sync video streams zoned output through DreamView instead of ptReal.
        self.streaming = False
        # Light -> time DreamView was last switched on; kept until it is switched off.
        self.razer = {}
        # Light -> time of the last mode or frame the lamp accepted in DreamView;
        # dropped when the lamp may have left the mode on its own.
        self.razer_frame = {}
        # Light -> time its status first went missing or off, while it still is.
        self.status_lost = {}
        # Device key -> (last identity-verified status, time). Kept through a
        # missed reply so status readers can tell a short miss from a loss;
        # output still requires a fresh verified status.
        self.verified = {}
        # Light -> (brightness, time) last sent while streaming. Video output
        # re-sends every frame (last_sent is cleared each tick), but an unchanged
        # brightness must not take a frame slot.
        self.stream_brightness = {}
        self.next_send = {}
        self.ids = {lan.device_key(item) for item in self.targets}
        if len(self.ids) != len(self.targets):
            raise ValueError("Pinned IDs must be distinct")
        self.sender, self.clock, self.wall_clock = sender, clock, wall_clock
        self.lock = threading.RLock()
        self.statuses, self.jobs, self.accepted, self.last_sent, self.last_refresh = (
            {},
            {},
            {},
            {},
            {},
        )
        self.commands_sent = 0
        self.pause_gate = None
        self.skipped_state = {light: 0 for light in self.by_light}
        self.rejected_sends = {light: 0 for light in self.by_light}
        self.color_samples = {light: deque(maxlen=500) for light in self.by_light}

    def razer_idle_seconds(self, light):
        return self.caps[light].get("razer_idle_seconds", RAZER_IDLE_SECONDS)

    def razer_lost_seconds(self, light):
        return self.caps[light].get("razer_lost_seconds", RAZER_LOST_SECONDS)

    def set_streaming(self, active):
        """Use DreamView for zoned video output; scene output switches it off first."""
        with self.lock:
            if self.streaming != bool(active):
                self.streaming = bool(active)
                self.last_sent.clear()
                self.stream_brightness.clear()
                # A ptReal slot can lie seconds ahead; it must not hold the stream.
                self.next_send.clear()
                if self.streaming:
                    # Between streams the scene output can take a lamp out of
                    # DreamView, and the lamp does not report the mode, so every
                    # stream start switches it on again.
                    self.razer_frame.clear()

    def stream_held(self, light, now):
        """True while a streaming lamp's last verified On is recent enough for frames."""
        if not self.streaming or light not in self.razer:
            return False
        native, seen = self.verified.get(lan.device_key(self.by_light[light]), (None, None))
        return (native is not None and native.get("onOff") == 1
                and 0 <= now - seen <= lan.STREAM_HOLD_SECONDS)

    def update_statuses(self, statuses):
        with self.lock:
            for key, value in statuses.items():
                if key in self.ids and value[0] is not None:
                    self.statuses[key] = value
                    if value[0].get("identity_verified", True) is not False:
                        self.verified[key] = value
                    elif (value[0].get("lan_diagnostic") or {}).get(
                            "reason") != "native_status_missing":
                        # Only a missed status reply is held; a lost identity or
                        # a transport failure ends the held reading at once.
                        self.verified.pop(key, None)

    def power_observations(self):
        with self.lock:
            now = self.clock()
            result = {}
            for light, device in self.by_light.items():
                native, seen = self.statuses.get(lan.device_key(device), (None, 0))
                sample = native.get("native_sample") if isinstance(native, dict) else None
                valid = (isinstance(native, dict) and native.get("identity_verified") is True
                         and type(native.get("onOff")) is int and native["onOff"] in {0, 1}
                         and type(seen) in (int, float) and math.isfinite(seen)
                         and 0 <= now - seen <= 0.75
                         and type(sample) in (int, float) and math.isfinite(sample)
                         and 0 <= now - sample <= 0.75)
                result[light] = {"on": bool(native["onOff"]) if valid else None,
                                "sample": sample if valid else None}
            return result

    def included(self, light):
        name = "present" if self.configuration == "scene" else "enabled"
        member = getattr(self.layout_store, name, None)
        return member(light) if member is not None else True

    def brightness_scale_for(self, light, base):
        """Return this configuration's brightness correction for a light."""
        name = "scene_scale" if self.configuration == "scene" else "scale"
        return getattr(self.layout_store, name, self.layout_store.scale)(light, base)

    def scene_gains(self, light):
        """Return seven bottom-to-top scene fade gains, or None when nothing is faded."""
        loader = getattr(self.layout_store, "scene_fade", None)
        if self.configuration != "scene" or loader is None:
            return None
        fade = loader()
        gains = fade.get(light) if fade else None
        return gains if gains is not None and min(gains) < 1 else None

    def fade_target(self, light, target):
        """Attenuate a scene target by the scene fade without mutating the job."""
        gains = self.scene_gains(light)
        if gains is None:
            return target
        if "segments" in target or self.by_light[light].get("segment_count", 0) == 7:
            segments = target.get("segments") or [as_rgb(target)] * 7
            target = {key: value for key, value in target.items()
                      if key not in ("rgb", "kelvin")}
            target["segments"] = [
                tuple(round(value * gain) for value in rgb)
                for rgb, gain in zip(segments, gains)
            ]
        else:
            # Without segments the strip's mean gain dims the whole lamp.
            mean = sum(gains) / len(gains)
            target["brightness"] = max(1, min(100, round(target["brightness"] * mean)))
        return target

    def clear_light(self, light):
        """Forget output work while retaining the accepted source ordering fence."""
        with self.lock:
            for values in (self.jobs, self.last_sent, self.last_refresh, self.next_send):
                values.pop(light, None)

    def target(self, light):
        with self.lock:
            if not self.included(light):
                self.clear_light(light)
                return None
            job = self.jobs.get(light)
            if job is None:
                return None
            fraction = (self.clock() - job["start"]) / job["duration"] if job["duration"] else 1
            return blend(job["from"], job["to"], fraction)

    def effective_target(self, light):
        target = self.target(light)
        if target is not None and self.layout_store is not None:
            base = lan.parse_brightness_scale(self.by_light[light].get("brightness_scale", 1))
            scale = self.brightness_scale_for(light, base)
            brightness = (
                target["brightness"] * scale / base
                if base
                else self.jobs[light].get("source_bri", 254) / 254 * 100 * scale
            )
            target["brightness"] = max(1, min(100, round(brightness)))
            target = self.fade_target(light, target)
        return target

    def submit(self, payload):
        if not isinstance(payload, dict) or set(payload) != {
            "light",
            "state",
            "transitiontime",
            "session",
            "sequence",
            "issued_at",
        }:
            raise ValueError("Invalid command shape")
        light = payload["light"]
        if light not in self.by_light:
            raise ValueError("Unknown light")
        session = payload["session"]
        sequence = payload["sequence"]
        if not isinstance(session, str) or not 1 <= len(session) <= 80:
            raise ValueError("Invalid command session")
        if type(sequence) is not int or not 0 <= sequence < 2**63:
            raise ValueError("Invalid command sequence")
        issued = number(payload["issued_at"], self.wall_clock() - 5, self.wall_clock() + 5)
        duration = number(payload["transitiontime"], 0, 1200) / 10
        device = self.by_light[light]
        desired = normalize_state(
            payload["state"], device.get("brightness_scale", 1), device.get("segment_count", 0)
        )
        with self.lock:
            previous = self.accepted.get(light)
            if previous and (
                (session == previous[0] and sequence <= previous[1])
                or (session != previous[0] and issued <= previous[2])
            ):
                return False
            self.accepted[light] = (session, sequence, issued)
            if not desired["on"] or not self.included(light):
                self.clear_light(light)
                return True
            initial = self.target(light)
            if initial is None:
                status = self.statuses.get(lan.device_key(device), (None, 0))[0]
                initial = native_target(status, desired)
                if self.layout_store is not None and status and "brightness" in status:
                    base = lan.parse_brightness_scale(device.get("brightness_scale", 1))
                    scale = self.brightness_scale_for(light, base)
                    gains = self.scene_gains(light)
                    if gains is not None and self.by_light[light].get("segment_count", 0) != 7:
                        # The native brightness already carries the mean fade gain.
                        scale *= sum(gains) / len(gains)
                    if base and scale:
                        initial["brightness"] *= base / scale
            self.jobs[light] = {
                "from": initial,
                "to": desired,
                "start": self.clock(),
                "duration": duration,
                "source_bri": payload["state"].get("bri", 254),
            }
        return True

    def render(self):
        with self.lock:
            for light in self.by_light:
                if not self.included(light):
                    self.clear_light(light)
            if self.pause_gate is not None and not self.pause_gate():
                self.last_sent.clear()
                return
            now = self.clock()
            for light, device in self.by_light.items():
                if not self.included(light):
                    self.clear_light(light)
                    continue
                if self.power_owner is not None:
                    revision = self.power_owner.revision(light)
                    if self.power_revisions.get(light) != revision:
                        self.last_sent.pop(light, None)
                        self.stream_brightness.pop(light, None)
                        self.power_revisions[light] = revision
                    if not self.power_owner.allowed(light):
                        self.last_sent.pop(light, None)
                        if light in self.jobs:
                            self.skipped_state[light] += 1
                        continue
                status, seen = self.statuses.get(lan.device_key(device), (None, float("-inf")))
                held = False
                if not status or status.get("onOff") != 1 or not 0 <= now - seen <= 0.75:
                    lost = self.status_lost.setdefault(light, now)
                    verified_off = (
                        status is not None and 0 <= now - seen <= 0.75
                        and status.get("onOff") == 0
                        and status.get("identity_verified", True) is not False
                    )
                    if verified_off or now - lost >= self.razer_lost_seconds(light):
                        # An Off or a power cycle ends DreamView on the lamp.
                        self.razer_frame.pop(light, None)
                    held = not verified_off and self.stream_held(light, now)
                    if not held:
                        self.last_sent.pop(light, None)
                        if light in self.jobs:
                            self.skipped_state[light] += 1
                        continue
                else:
                    self.status_lost.pop(light, None)
                if not self.streaming and light in self.razer:
                    # The lamp ignores other color commands while DreamView is on,
                    # so leave it even when no scene target is waiting yet.
                    try:
                        left = self.sender(device, "razer", razer_mode(False))
                    except OSError:
                        left = False
                    if not left:
                        # Keep the scene target pending until the lamp leaves DreamView.
                        self.rejected_sends[light] += 1
                        continue
                    self.razer.pop(light)
                    self.razer_frame.pop(light, None)
                target = self.effective_target(light)
                if target is None:
                    continue
                old = self.last_sent.get(light, {})
                refresh = now - self.last_refresh.get(light, float("-inf")) >= 30
                updates = []
                stream = self.streaming and "segments" in target
                if held and not stream:
                    self.last_sent.pop(light, None)
                    self.skipped_state[light] += 1
                    continue
                if stream:
                    commands = [
                        ("razer", razer_data(target["segments"])),
                        ("brightness", {"value": target["brightness"]}),
                    ]
                elif "segments" in target:
                    commands = [
                        ("ptReal", segment_data(target["segments"])),
                        ("brightness", {"value": target["brightness"]}),
                    ]
                else:
                    commands = lan.govee_commands(target)
                for name, data in commands:
                    if name == "brightness" and stream:
                        sent = self.stream_brightness.get(light)
                        # Another writer, such as the scene output at a Sync start,
                        # can leave the lamp at its own brightness; the fresh status
                        # shows it, so the stream brightness is sent again.
                        changed = (
                            sent is None or sent[0] != target["brightness"]
                            or (type(status.get("brightness")) is int
                                and status["brightness"] != target["brightness"]
                                and now - sent[1] >= 1)
                        )
                    else:
                        changed = (
                            old.get("brightness") != target["brightness"]
                            if name == "brightness"
                            else (old.get("rgb"), old.get("kelvin"), old.get("segments"))
                            != (target.get("rgb"), target.get("kelvin"), target.get("segments"))
                        )
                    if changed or refresh:
                        updates.append((name, data))
                mode = None
                if stream and (light not in self.razer or light not in self.razer_frame
                               or now - self.razer_frame[light] >= self.razer_idle_seconds(light)):
                    mode = razer_mode(True)
                if mode is not None:
                    # The lamp stays dark after the mode switch until a frame arrives,
                    # so the current frame always follows it in the same burst.
                    frame = ("razer", commands[0][1])
                    updates = [("razer", mode), frame] + [
                        update for update in updates if update[0] != "razer"]
                if stream:
                    rate = self.stream_rates[light]
                else:
                    rate = self.packet_rates[light]
                    gradient_rate = self.gradient_packet_rates[light]
                    if "segments" in target and gradient_rate is not None:
                        rate = min(rate, gradient_rate) if rate is not None else gradient_rate
                if held:
                    # Only DreamView packets ride out a status miss; a brightness
                    # change waits for a fresh status.
                    updates = [update for update in updates if update[0] == "razer"]
                if stream and any(name == "brightness" for name, _ in updates):
                    # In DreamView the lamp shows a new brightness only with the
                    # next frame, so the current frame follows the brightness;
                    # otherwise a still picture keeps the old brightness.
                    frame_data = commands[0][1]
                    updates = [update for update in updates if update[1] is not frame_data]
                    updates.append(("razer", frame_data))
                if (
                    updates
                    and rate is not None
                    and now + 1e-9 < self.next_send.get(light, float("-inf"))
                ):
                    continue
                complete = True
                for name, data in updates:
                    try:
                        success = self.sender(device, name, data)
                    except OSError:
                        success = False
                    complete = success and complete
                    if success and data is mode:
                        self.razer[light] = now
                    if success and stream and name == "razer":
                        self.razer_frame[light] = now
                    if success and stream and name == "brightness":
                        self.stream_brightness[light] = (data["value"], now)
                    self.commands_sent += int(success)
                    if not success:
                        self.rejected_sends[light] += 1
                    elif name == "colorwc":
                        self.color_samples[light].append(
                            {
                                "time": now,
                                "color": data["color"],
                                "kelvin": data["colorTemInKelvin"],
                            }
                        )
                if updates and rate is not None:
                    interval = len(updates) / rate
                    previous = self.next_send.get(light)
                    if stream and previous is not None and 0 <= now - previous < interval:
                        # Keep the frame cadence: a render tick that came a little
                        # late must not stretch every later slot.
                        self.next_send[light] = previous + interval
                    else:
                        self.next_send[light] = now + interval
                if complete:
                    self.last_sent[light] = target
                    if refresh:
                        self.last_refresh[light] = now

    def snapshot(self):
        with self.lock:
            return {
                "active_lights": sorted(self.jobs),
                "streaming": self.streaming,
                "dreamview_lights": sorted(self.razer),
                "commands_sent": self.commands_sent,
                "skipped_state": dict(self.skipped_state),
                "rejected_sends": dict(self.rejected_sends),
                "color_samples": {
                    light: list(samples) for light, samples in self.color_samples.items()
                },
                "targets": [
                    {
                        "light": item["light"],
                        "tier": self.caps[item["light"]]["tier"],
                        "device": lan.device_key(item),
                        "brightness_scale": item.get("brightness_scale", 1),
                        "segment_count": item.get("segment_count", 0),
                        "max_commands_per_second": self.packet_rates[item["light"]],
                        "max_gradient_packets_per_second":
                            self.gradient_packet_rates[item["light"]],
                        "target": self.effective_target(item["light"]),
                        "native": self.statuses.get(lan.device_key(item), (None, 0))[0],
                        "status_age": (
                            self.clock() - self.statuses[lan.device_key(item)][1]
                            if lan.device_key(item) in self.statuses
                            else None
                        ),
                        "verified_native": copy.deepcopy(
                            self.verified.get(lan.device_key(item), (None, 0))[0]),
                        "verified_age": (
                            self.clock() - self.verified[lan.device_key(item)][1]
                            if lan.device_key(item) in self.verified
                            else None
                        ),
                    }
                    for item in self.targets
                ],
            }


def prepare_targets(targets, composition_profile):
    """Give every target the segment profile of its model tier, in place.

    A segmented model gets its segment count when a Sync profile exists (or the
    gradient mode is on). A light forced to the basic tier never has segments. Any
    other model is basic unless its stored target declares ``segment_count`` itself;
    nothing is enabled for it automatically.
    """
    for device in targets:
        caps = target_capabilities(device)
        segments = caps["segments"]
        # A target without a model predates the capability table: as before, only the
        # gradient mode enables its segments.
        modelled = bool(device.get("sku") or device.get("model"))
        auto_segments = composition_profile is not None and segments > 0 and modelled
        if caps["tier"] == "basic":
            # A basic light gets one color for the whole lamp, whatever count is stored.
            device["segment_count"] = 0
        elif segments and device.get("segment_count", 0) != segments and (
                GRADIENT_MODE or auto_segments):
            device["segment_count"] = segments
            if auto_segments:
                device.setdefault("max_gradient_packets_per_second",
                                  caps["max_gradient_packets_per_second"])
        else:
            device.setdefault("segment_count", 0)


def registry_lights(registry, controller):
    """The enrolled lights for Light Studio: id, provider, model and zone count.

    A Hue light has one zone per channel; a Govee light one per active segment, or one
    when it is driven as a single color. Device identities and addresses stay here.
    """
    native = registry.native_profile()
    channels = native.channels if native is not None else {}
    result = []
    for item in registry.to_dict()["lights"]:
        light, provider = item["id"], item["provider"]
        if provider == "hue":
            zones, model = len(channels.get(light, ())), item["hue"].get("model")
        else:
            target = controller.by_light.get(light, {}) if controller is not None else {}
            zones = target.get("segment_count", 0)
            model = item["govee"].get("model") or item["govee"].get("sku")
        result.append({"id": light, "provider": provider,
                       "model": model if isinstance(model, str) else None,
                       "zones": zones if type(zones) is int and zones > 0 else 1,
                       "migrated": not light.startswith("device-")})
    return result


def handler_for(controller, token, hue_proxies=None, video_output=None,
                power_owner=None, power_token=None, service_info=None, catalog=None, preview=None,
                power_requests=None, homekit=None, identify=None, sync_off=None,
                calibration=None, lights=None):
    if power_owner is not None and (
        not isinstance(power_token, str) or not power_token
        or hmac.compare_digest(power_token.encode(), token.encode())
    ):
        raise ValueError("A distinct power owner credential is required")
    broker = power_requests or (PowerRequests(power_owner) if power_owner is not None else None)
    signatures = SignatureVerifier(power_token) if homekit is not None else None
    # Native Hue light id (registry id) -> its proxy.
    hue_proxies = hue_proxies or {}

    class Handler(BaseHTTPRequestHandler):
        power_requests = broker
        def log_message(self, *args):
            pass

        def respond(self, status, value):
            data = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            nonce = getattr(self, "signed_nonce", None)
            if nonce is not None:
                self.send_header("X-LCC-Response",
                                 response_signature(power_token, nonce, status, data))
            keep_alive = getattr(self, "stream_keep_alive", False)
            self.send_header("Connection", "keep-alive" if keep_alive else "close")
            self.close_connection = not keep_alive
            if keep_alive:
                self.connection.settimeout(2)
            self.end_headers()
            self.wfile.write(data)

        def authorized(self):
            return hmac.compare_digest(self.headers.get("X-Adapter-Token", "").encode(),
                                       token.encode())

        def owner_authorized(self):
            return power_token is not None and hmac.compare_digest(
                self.headers.get("X-Power-Owner-Token", "").encode(), power_token.encode())

        def do_GET(self):
            self.stream_keep_alive = False
            self.signed_nonce = None
            if self.path == "/homekit" and homekit is not None:
                if not signatures.verify("GET", self.path, self.headers, b""):
                    return self.respond(403, {"error": "unauthorized"})
                self.signed_nonce = self.headers["X-LCC-Nonce"]
            elif not self.authorized():
                return self.respond(403, {"error": "unauthorized"})
            if self.path == "/homekit":
                if homekit is None:
                    return self.respond(404, {"error": "not found"})
                return self.respond(200, homekit.wire_state())
            if self.path == "/discovery" and catalog is not None:
                return self.respond(200, catalog.snapshot())
            if self.path not in ("/status", "/diagnostics"):
                return self.respond(404, {"error": "not found"})
            result = controller.snapshot()
            loader = getattr(getattr(controller, "layout_store", None), "sync_profile", None)
            profile = loader() if loader is not None else None
            if profile is not None:
                result["composition"] = {
                    "enabled_roles": profile["enabled_roles"], "signature": profile["signature"],
                    "lights": sorted(profile["lights"]),
                }
            if hue_proxies:
                result["hue"] = {light: proxy.snapshot() for light, proxy in hue_proxies.items()}
            if getattr(lights, "registry", None) is not None:
                result["lights"] = registry_lights(lights.registry, controller)
            if video_output is not None:
                result["video"] = video_output.snapshot()
            if power_owner is not None:
                result["power_owner"] = power_owner.snapshot()
            if service_info is not None:
                result["service"] = dict(service_info)
            if sync_off is not None:
                result["sync_off"] = sync_off.snapshot()
            if calibration is not None:
                result["calibration"] = calibration.snapshot()
            if preview is not None:
                try:
                    output_snapshot = preview.snapshot()
                except Exception:
                    output_snapshot = {}
                result["preview"] = build_preview(result, output_snapshot)
            if self.path == "/diagnostics":
                # Allowlisted export: no credentials, addresses or device identifiers.
                return self.respond(200, export_diagnostics(result))
            self.respond(200, result)

        def do_POST(self):
            self.stream_keep_alive = False
            self.signed_nonce = None
            if self.path in ("/power-owner", "/homekit"):
                # The local HomeKit authority fences the legacy remote owner.
                if (power_owner is None or power_token is None
                        or (self.path == "/power-owner") is not (homekit is None)):
                    return self.respond(404, {"error": "not found"})
                # /homekit is signed; its signature covers the body read below.
                if self.path == "/power-owner" and not self.owner_authorized():
                    return self.respond(403, {"error": "unauthorized"})
            elif not self.authorized():
                return self.respond(403, {"error": "unauthorized"})
            if self.path not in ("/command", "/stream", "/power-owner", "/power-request",
                                 "/native-power", "/identify", "/calibration",
                                 "/calibration/end", "/homekit", "/lights/enroll",
                                 "/lights/remove") or (
                self.path == "/stream" and video_output is None
            ) or (self.path == "/identify" and identify is None) or (
                self.path.startswith("/lights/") and lights is None) or (
                self.path.startswith("/calibration") and calibration is None):
                return self.respond(404, {"error": "not found"})
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 8192:
                    raise ValueError("Invalid request size")
                raw = self.rfile.read(size)
                if self.path == "/homekit":
                    if not signatures.verify("POST", self.path, self.headers, raw):
                        return self.respond(403, {"error": "unauthorized"})
                    self.signed_nonce = self.headers["X-LCC-Nonce"]
                payload = json.loads(raw)
                if self.path == "/power-owner":
                    # The overlay still names the lamps by side; translate at this boundary.
                    payload = owner_envelope(payload)
                    capability = validate_capability(payload.get("power_requests"),
                                                     payload.get("states"))
                    envelope = {key: value for key, value in payload.items()
                                if key != "power_requests"}
                    accepted = power_owner.submit(envelope)
                    observed = controller.power_observations()
                    return self.respond(200, {"accepted": accepted,
                                              "power_request_version": 1,
                                              "observer_epoch": controller.observer_epoch,
                                              "observed": owner_observed(observed),
                                              **owner_exchange(broker.exchange(
                                                  envelope, capability, observed, accepted))})
                elif self.path == "/homekit":
                    confirmed = homekit.set(homekit.wire_intent(payload))
                    return self.respond(200 if confirmed else 503,
                                        {"accepted": confirmed, **homekit.wire_state()})
                elif self.path == "/power-request":
                    if broker is None:
                        return self.respond(503, {"accepted": False})
                    accepted = broker.request(payload)
                    if sync_off is not None:
                        # An owner power change during Sync wins over the Sync hold.
                        for role in payload["states"]:
                            sync_off.owner_power(role)
                    return self.respond(200, {"accepted": accepted})
                elif self.path == "/identify":
                    if (not isinstance(payload, dict) or set(payload) != {"role"}
                            or not isinstance(payload["role"], str)):
                        raise ValueError("Invalid identify request")
                    try:
                        identify(payload["role"])
                    except IdentifyRefused as refused:
                        return self.respond(409, {"accepted": False, "reason": refused.reason})
                    except IdentifyFailed:
                        return self.respond(503, {"accepted": False})
                    return self.respond(200, {"accepted": True})
                elif self.path == "/calibration":
                    try:
                        return self.respond(200, calibration.update(payload))
                    except CalibrationRefused as refused:
                        return self.respond(409, {"active": False, "reason": refused.reason})
                elif self.path in ("/lights/enroll", "/lights/remove"):
                    if not isinstance(payload, dict) or set(payload) != {"id"}:
                        raise ValueError("Invalid light request")
                    change = lights.enroll if self.path == "/lights/enroll" else lights.remove
                    try:
                        light = change(payload["id"])
                    except LightRefused as refused:
                        return self.respond(409, {"accepted": False, "reason": refused.reason})
                    except OSError:
                        return self.respond(503, {"accepted": False})
                    # The adapter restarts with the new light set (see light_enrollment).
                    # An enrollment names the registry id: a migrated lamp that
                    # rejoins gets its migrated id back.
                    answer = {"accepted": True, "restarting": True}
                    if light is not None:
                        answer["id"] = light
                    return self.respond(200, answer)
                elif self.path == "/calibration/end":
                    if payload != {}:
                        raise ValueError("Invalid calibration end request")
                    calibration.end("ended")
                    return self.respond(200, {"active": False})
                elif self.path == "/native-power":
                    wanted = payload.get("lights") if isinstance(payload, dict) else None
                    if (not isinstance(payload, dict) or set(payload) != {"lights"}
                            or not isinstance(wanted, dict) or not wanted
                            or set(wanted) - set(hue_proxies)
                            or any(type(value) is not bool for value in wanted.values())):
                        raise ValueError("Invalid native power request")
                    if sync_off is not None:
                        for light in wanted:
                            sync_off.owner_power(light)
                    try:
                        confirmed = all([hue_proxies[light].set_power(value)
                                         for light, value in wanted.items()])
                    except (OSError, ValueError, KeyError):
                        confirmed = False
                    return self.respond(200 if confirmed else 503, {"accepted": confirmed})
                elif self.path == "/stream":
                    # No Sync session starts while the light set is being changed.
                    accepted = (video_output.submit(payload) if lights is None
                                else lights.stream(lambda: video_output.submit(payload)))
                    # Only an authenticated, fully consumed stream envelope may
                    # reuse its connection. Other routes retain legacy closure.
                    self.stream_keep_alive = (
                        self.headers.get("Connection", "").lower() == "keep-alive")
                elif isinstance(payload, dict) and payload.get("light") in hue_proxies:
                    accepted = hue_proxies[payload["light"]].submit(payload)
                else:
                    accepted = controller.submit(payload)
                self.respond(200, {"accepted": accepted})
            except PowerRequestUnavailable:
                self.respond(503, {"accepted": False})
            except (ValueError, TypeError, KeyError):
                self.respond(400, {"error": "invalid command"})

    return Handler


def main(argv=None, service_info=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--port", type=int, default=19880)
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--hue-config", type=Path)
    parser.add_argument("--hue-play", action="store_true")
    parser.add_argument("--hue-stream-config", type=Path)
    parser.add_argument("--power-owner-token-file", type=Path)
    parser.add_argument("--power-authority", choices=("owner", "homekit"), default="owner",
                        help="owner: remote heartbeat; homekit: this adapter executes power")
    args = parser.parse_args(argv)
    if args.hue_play and not args.hue_config:
        parser.error("--hue-play requires --hue-config")
    if args.hue_stream_config and not args.hue_play:
        parser.error("--hue-stream-config requires --hue-play and --hue-config")
    state_dir = args.state_dir or args.registry.parent
    if (state_dir / "live-output-hold").exists():
        raise SystemExit("Owner live-output hold is active; physical adapter remains stopped")
    registry = LightRegistry.load(args.registry)
    native_profile = registry.native_profile()
    if native_profile and not args.hue_config:
        parser.error("A native Hue profile in the light registry requires --hue-config")
    # A paired Hue area may have no Hue light now: the owner removed them all and
    # can enroll one again; until then Sync drives the Govee lights only.
    paired_area = registry.to_dict().get("hue", {}).get("area_id")
    if args.hue_config and not native_profile and paired_area is None:
        parser.error("--hue-config requires a paired Hue area in the light registry")
    config = {"targets": registry.govee_targets()}
    layout_store = LayoutStore(
        state_dir / "desk-layout.json",
        native={light: len(channels) for light, channels in native_profile.channels.items()
                if light not in NATIVE_SOURCES} if native_profile else None,
        migrated={light for light in native_profile.channels if light in NATIVE_SOURCES}
        if native_profile else frozenset())
    composition_profile = layout_store.sync_profile()
    prepare_targets(config["targets"], composition_profile)
    if not args.token_file.exists():
        descriptor = os.open(args.token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(secrets.token_hex(24) + "\n")
    token = args.token_file.read_text().strip()
    power_owner = power_token = homekit = None
    power_requests = None
    preview = OutputPreviewStore(
        native_channels=native_profile.channels if native_profile else {})
    # Per-lamp calibration converts commands to device values on the way out and
    # statuses back to logical values; an invalid profile refuses to start.
    calibration = GoveeCalibration(config["targets"], layout_store)
    sender = calibration.wrap(lan.send_to_target)
    power_sender = calibration.wrap(lan.send_power_to_target)
    if args.power_owner_token_file:
        power_token = args.power_owner_token_file.read_text().strip()
        if (not 32 <= len(power_token) <= 128
                or any(c not in "0123456789abcdef" for c in power_token)
                or hmac.compare_digest(power_token.encode(), token.encode())):
            raise ValueError("Invalid power owner credential")
        if args.power_authority == "homekit":
            idle = {}
            power_owner = homekit = HomeKitPower(
                config["targets"], state_dir / "homekit-power.json", power_sender,
                observe=lambda: controller.power_observations(),
                idle=lambda light: idle["check"](light), names=layout_store.names,
                release=lambda light: idle["release"](light))
            power_requests = HomeKitRequests(homekit)
        else:
            power_owner = PowerOwner(config["targets"])
            power_owner.attach_power(power_sender,
                                     observe=lambda: controller.power_observations())
            power_requests = PowerRequests(
                power_owner, observe=lambda: controller.power_observations())
        sender = power_owner.wrap(sender)
    elif args.power_authority == "homekit":
        raise ValueError("HomeKit power requires a power owner credential")
    controller = GoveeController(config["targets"],
                                 sender=preview.wrap_govee(sender, "scene-output"),
                                 layout_store=layout_store,
                                 power_owner=power_owner,
                                 configuration="scene")
    hue_proxies = {}
    hue_config = None
    if args.hue_config:
        hue_config = json.loads(args.hue_config.read_text())
        # One proxy per native light of the profile, keyed by its registry id.
        for light, resource in (native_profile.light_ids if native_profile else {}).items():
            hue_proxies[light] = HueStripProxy(
                PinnedHueClient(hue_config, resource_id=resource, profile=native_profile),
                light,
                layout_store=layout_store,
                preview=preview,
                profile=native_profile,
            )
    video = None
    if args.hue_stream_config:
        stream_config = json.loads(args.hue_stream_config.read_text())
        if stream_config.get("bridge") != hue_config["bridge"]:
            raise ValueError("Stream and scene bridge must match")
        video = VideoOutput(
            GoveeController(config["targets"],
                            sender=preview.wrap_govee(sender, "sync-output"),
                            layout_store=layout_store,
                            power_owner=power_owner),
            HueEntertainmentRelay(
                stream_config, layout_store=layout_store, preview=preview, profile=native_profile,
                location_cache_file=state_dir / "native-location-cache.json")
            if native_profile is not None else NoNativeRelay(),
            hue_proxies.values(),
            diagnostics=StreamDiagnostics(state_dir / 'adapter-stream-health.jsonl'),
        )
        for proxy in hue_proxies.values():
            proxy.output_lock = video.lock
            proxy.write_gate = lambda: not video.active()
    identify = Identify(controller, hue_proxies, video=video, power_owner=power_owner,
                        layout_store=layout_store)
    identify.sender = sender
    controller.sender = identify.wrap(controller.sender)
    if video is not None:
        video.controller.sender = identify.wrap(video.controller.sender)
    # Light Studio's colour calibration holds the lamps it shows the test white on.
    calibration_session = CalibrationSession(controller, calibration, hue_proxies,
                                             video=video, power_owner=power_owner)
    calibration_session.sender = sender
    controller.sender = calibration_session.wrap(controller.sender)
    if video is not None:
        video.controller.sender = calibration_session.wrap(video.controller.sender)
    for proxy in hue_proxies.values():
        proxy.write_gate = calibration_session.hold_native(proxy.write_gate)
    sync_off = SyncOffSession(
        layout_store, (lambda: video.active()) if video is not None else (lambda: False),
        state_dir / SYNC_OFF_FILE, govee=power_owner, native=hue_proxies,
        observe=controller.power_observations)
    if homekit is not None:
        idle["check"] = lambda light: (
            (video is None or not video.active()) and light not in controller.jobs)

        def release_scene(light):
            # Sync output owns the lamp until it ends; only a Hue scene yields.
            if video is None or not video.active():
                controller.clear_light(light)

        idle["release"] = release_scene
    controller.pause_gate = lambda: (
        (video is None or not video.active())
        and all(proxy.govee_allowed() for proxy in hue_proxies.values() if proxy.included())
    )

    def remember_targets():
        # The adapter follows a lamp to a new address; persist it in the registry,
        # the only source of lights. A failed write is an OSError for the
        # transport, which restarts its receive loop as it did for the old file.
        enrollment.remember(controller.targets)

    legacy_roles = {("govee", lan.device_key(device)): device["light"]
                    for device in controller.targets if device["light"] in LEGACY_SIDES}
    # Only the migrated Hue lights own fixed layout rows; an enrolled one is placed
    # like any discovered light.
    for light, proxy in hue_proxies.items():
        resource = getattr(proxy, "resource_id", None)
        if resource and light in NATIVE_SOURCES:
            legacy_roles[("hue", resource)] = light
    # A removed migrated lamp is found under its migrated id, so Light Studio can
    # connect its layout row again.
    rejoin_roles = {(record["identity"]["provider"], record["identity"]["key"]): record["id"]
                    for record in registry.to_dict().get("retired", [])
                    if "identity" in record and not record["id"].startswith("device-")}
    catalog = DeviceCatalog(legacy_roles=legacy_roles, rejoin_roles=rejoin_roles)
    catalog.provider_state("govee", "discovering")
    catalog.provider_state("hue", "discovering" if hue_config is not None else "not_configured")
    stop = threading.Event()

    def restart_with_new_lights():
        print("Light registry changed; restarting the adapter.", flush=True)
        # Leave time for the HTTP answer; Docker restarts the finished container.
        threading.Timer(0.5, stop.set).start()

    # A new Hue light is enrolled into the paired Entertainment area (#60 chooses it later).
    # Without usable paired credentials a Hue enrollment is refused as unavailable.
    try:
        area = (HueArea(hue_config, paired_area,
                        pin_file=state_dir / "hue-bridge-id")
                if hue_config is not None else None)
    except (KeyError, TypeError, ValueError):
        area = None
    enrollment = LightEnrollment(
        args.registry, registry, catalog,
        busy=lambda: video is not None and video.owns_lights(),
        restart=restart_with_new_lights,
        area=area)
    server = ThreadingHTTPServer(
        (args.bind, args.port),
        handler_for(
            controller,
            token,
            hue_proxies,
            video_output=video,
            power_owner=power_owner,
            power_token=power_token,
            service_info=service_info,
            catalog=catalog,
            preview=preview,
            power_requests=power_requests,
            homekit=homekit,
            identify=identify,
            sync_off=sync_off,
            calibration=calibration_session,
            lights=enrollment,
        ),
    )
    video_thread = http_thread = None
    video_stop = threading.Event()

    def publish_statuses(statuses):
        statuses = calibration.decode(statuses)
        controller.update_statuses(statuses)
        if video is not None:
            video.controller.update_statuses(statuses)
        if homekit is not None:
            homekit.tick()

    try:
        if hue_config is not None:
            reader = HueDiscoveryReader(hue_config, catalog)
            threading.Thread(target=reader.run, args=(stop,), daemon=True,
                             name="hue-discovery").start()
        for light, proxy in hue_proxies.items():
            threading.Thread(
                target=proxy.run, args=(stop,), daemon=True, name="adapter-" + light
            ).start()
        if video is not None:
            video_thread = threading.Thread(
                target=video.run, args=(video_stop,), name="adapter-video"
            )
            video_thread.start()
        threading.Thread(target=sync_off.run, args=(stop,), daemon=True,
                         name="adapter-sync-off").start()
        threading.Thread(target=calibration_session.run, args=(stop,), daemon=True,
                         name="adapter-calibration").start()
        transport = GoveeTransport(
            controller.targets,
            publish_statuses,
            remember=remember_targets,
            discovery=catalog,
        )
        threading.Thread(
            target=transport.run, args=(stop,), daemon=True, name="adapter-status"
        ).start()
        http_thread = threading.Thread(
            target=server.serve_forever, daemon=True, name="adapter-http"
        )
        http_thread.start()
        print(
            f"Live adapter on port {args.port}; {len(controller.targets)} pinned Govee; "
            + ("HomeKit power authority." if homekit is not None else "no power commands."),
            flush=True,
        )
        deadline = time.monotonic()
        while not stop.is_set():
            if video is not None and video.active():
                video.render()
                with controller.lock:
                    controller.last_sent.clear()
            else:
                controller.render()
            deadline += 0.05
            delay = deadline - time.monotonic()
            if delay < 0:
                deadline = time.monotonic()
            stop.wait(max(0, delay))
    except KeyboardInterrupt:
        print("Live adapter stopped.", flush=True)
    finally:
        # Restore lamps switched off for Sync while native readings are still live.
        sync_off.close()
        stop.set()
        if power_requests is not None:
            power_requests.close()
        if http_thread is not None and http_thread.is_alive():
            server.shutdown()
        server.server_close()
        if video_thread is not None and video_thread.ident is not None:
            video.lease.close()
            video_stop.set()
            video_thread.join()
            for proxy in hue_proxies.values():
                proxy.process_once()
            with controller.lock:
                controller.last_sent.clear()
            controller.render()


if __name__ == "__main__":
    main()
