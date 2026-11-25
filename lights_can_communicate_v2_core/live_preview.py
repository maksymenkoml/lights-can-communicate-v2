"""Bounded, passive telemetry for emitted commands, not physical acknowledgement."""

import base64
import copy
import math
import re
import threading
import time
from functools import reduce
from operator import xor

import lights_can_communicate_v2 as lan

# Canonical native channels of the migrated Hue lights (NativeHueProfile.channels).
MIGRATED_NATIVE_CHANNELS = {"play-center": (0,), "play-left": (1,), "play-right": (2,),
                            "hue-monitor": tuple(range(3, 12))}
HUE_LAMPS = tuple(MIGRATED_NATIVE_CHANNELS)
LAMP_IDS = ("govee-left", "govee-right", "hue-monitor", "play-left", "play-center",
            "play-right")
ROLE_LAMPS = {'left': 'govee-left', 'right': 'govee-right',
              **{lamp: lamp for lamp in HUE_LAMPS}}
# Registry light ids: the six migrated ids and enrolled devices.
LIGHT_ID = re.compile(r"(?:govee-left|govee-right|hue-monitor|play-left|play-center|play-right"
                      r"|device-[0-9a-f]{32})\Z")
MAX_LAMPS = 32
SOURCES = ("scene-output", "sync-output")
GOVEE_FRESH_SECONDS = 0.75
# A Govee lamp under DreamView misses a few status replies in a row now and
# then; its last verified reading stands this long (as in bridge/sync_health.py).
GOVEE_HOLD_SECONDS = 2


def _number(value, low, high):
    return type(value) in (int, float) and low <= value <= high and math.isfinite(value)


def _govee_reading(item):
    """Return a fresh verified Govee status, else a recently verified one."""
    for native, age, limit in (
        (item.get("native"), item.get("status_age"), GOVEE_FRESH_SECONDS),
        (item.get("verified_native"), item.get("verified_age"), GOVEE_HOLD_SECONDS),
    ):
        if (
            isinstance(native, dict)
            and native.get("identity_verified") is True
            and _number(age, 0, limit)
            and type(native.get("onOff")) is int
            and native["onOff"] in (0, 1)
        ):
            return native
    return None


def _rgb(value, maximum=255):
    if (
        not isinstance(value, (tuple, list))
        or len(value) != 3
        or any(type(channel) is not int or not 0 <= channel <= maximum for channel in value)
    ):
        raise ValueError("Invalid output RGB")
    return list(value)


def _temperature(kelvin):
    temperature = kelvin / 100
    red = 255 if temperature <= 66 else 329.698727446 * (temperature - 60) ** -0.1332047592
    green = (
        99.4708025861 * math.log(temperature) - 161.1195681661
        if temperature <= 66
        else 288.1221695283 * (temperature - 60) ** -0.0755148492
    )
    blue = (
        255 if temperature >= 66 else 138.5177312231 * math.log(temperature - 10) - 305.0447927307
    )
    return [max(0, min(255, round(value))) for value in (red, green, blue)]


def _segments(data):
    commands = data.get("command")
    if not isinstance(commands, list) or len(commands) != 7:
        raise ValueError("Expected seven output segments")
    colors = [None] * 7
    for command in commands:
        if not isinstance(command, str) or len(command) > 32:
            raise ValueError("Invalid output segment encoding")
        frame = base64.b64decode(command, validate=True)
        if (
            len(frame) != 20
            or frame[:4] != bytes([0x33, 0x05, 0x15, 0x01])
            or frame[7:12] != bytes(5)
            or frame[14:19] != bytes(5)
            or reduce(xor, frame, 0) != 0
        ):
            raise ValueError("Invalid output segment frame")
        mask = int.from_bytes(frame[12:14], "little")
        if mask not in (1, 2, 4, 8, 16, 32, 64):
            raise ValueError("Invalid output segment mask")
        index = mask.bit_length() - 1
        if colors[index] is not None:
            raise ValueError("Duplicate output segment")
        colors[index] = list(frame[4:7])
    return colors


def _razer(data):
    """Seven colors of a DreamView frame; None for a mode switch."""
    pt = data.get("pt")
    if not isinstance(pt, str) or len(pt) > 96:
        raise ValueError("Invalid DreamView encoding")
    raw = base64.b64decode(pt, validate=True)
    if (len(raw) < 6 or raw[:2] != bytes([0xBB, 0x00]) or raw[2] != len(raw) - 5
            or reduce(xor, raw[:-1], 0) != raw[-1]):
        raise ValueError("Invalid DreamView packet")
    if raw[3] == 0xB1:
        return None
    if raw[3] != 0xB0 or raw[5] != 7 or len(raw) != 7 * 3 + 7:
        raise ValueError("Invalid DreamView colors")
    return [list(raw[index:index + 3]) for index in range(6, 6 + 7 * 3, 3)]


def _xy(value):
    x, y = value["x"], value["y"]
    if not _number(x, 0, 1) or not _number(y, 0.00001, 1):
        raise ValueError("Invalid output chromaticity")
    return list(lan.xy_to_rgb(x, y))


class OutputPreviewStore:
    """Share only successful emitted color/brightness state across output paths."""

    def __init__(self, clock=time.monotonic, native_channels=None):
        self.clock = clock
        # Native light id -> its canonical channels; the adapter passes its profile's,
        # empty without Hue lights.
        self.native_channels = dict(MIGRATED_NATIVE_CHANNELS if native_channels is None
                                    else native_channels)
        self.lock = threading.RLock()
        self.outputs = {}

    def _update(self, lamp, source, colors=None, brightness=None):
        with self.lock:
            sample = self.outputs.setdefault(lamp, {})
            if colors is not None:
                if (
                    lamp in self.native_channels
                    and source == "scene-output"
                    and sample.get("brightness_source") == "sync-output"
                    and brightness is None
                ):
                    # Native frames carry an emitted gain, not the retained Hue dimmer.
                    sample.pop("brightness", None)
                    sample.pop("brightness_source", None)
                sample["colors"] = colors
                sample["color_source"] = source
            if brightness is not None:
                sample["brightness"] = brightness
                sample["brightness_source"] = source
            sample["source"] = source
            sample["seen"] = self.clock()

    def wrap_govee(self, sender, source):
        if source not in SOURCES:
            raise ValueError("Invalid output source")

        def send(device, command, data):
            result = sender(device, command, data)
            if result:
                try:
                    self._record_govee(device, command, data, source)
                except Exception:
                    # Telemetry must never affect transport results or producer workers.
                    pass
            return result

        return send

    def _record_govee(self, device, command, data, source):
        light = device.get("light")
        if not isinstance(light, str) or LIGHT_ID.match(light) is None or not isinstance(
                data, dict):
            return
        colors = brightness = None
        if command == "brightness":
            brightness = data.get("value")
            if not _number(brightness, 0, 100):
                return
        elif command == "colorwc":
            kelvin = data.get("colorTemInKelvin")
            color = data.get("color", {})
            rgb = _rgb([color.get(channel) for channel in ("r", "g", "b")])
            if kelvin == 0 and type(kelvin) is int:
                colors = [rgb]
            elif _number(kelvin, 2000, 9000):
                colors = [_temperature(kelvin)]
            else:
                return
        elif command == "ptReal":
            colors = _segments(data)
        elif command == "razer":
            colors = _razer(data)
            if colors is None:
                return
        else:
            return
        self._update(light, source, colors, brightness)

    def record_hue(self, role, body):
        if role not in self.native_channels or not isinstance(body, dict):
            return
        colors = brightness = None
        if "gradient" in body:
            points = body["gradient"]["points"]
            if not isinstance(points, list) or not 2 <= len(points) <= 5:
                raise ValueError("Invalid output gradient")
            colors = [_xy(point["color"]["xy"]) for point in points]
        elif "color_temperature" in body:
            mirek = body["color_temperature"]["mirek"]
            if not _number(mirek, 153, 500):
                raise ValueError("Invalid output temperature")
            colors = [_temperature(1_000_000 / mirek)]
        elif "color" in body:
            colors = [_xy(body["color"]["xy"])]
        if "dimming" in body:
            brightness = body["dimming"]["brightness"]
            if not _number(brightness, 0, 100):
                raise ValueError("Invalid output brightness")
        if colors is not None or brightness is not None:
            self._update(role, "scene-output", colors, brightness)

    def record_hue_read_back(self, role, native):
        """Record what a Hue light shows, read back from its bridge in scene mode.

        A scene set before the adapter started, or from another app, never passes
        through record_hue(), so without the read-back the preview showed such a lamp
        unlit. The light's v2 resource reports its color mode: a valid mirek is white,
        gradient points are a gradient, and otherwise the xy color applies.
        """
        if role not in self.native_channels or not isinstance(native, dict):
            return
        temperature = native.get("color_temperature")
        points = (native.get("gradient") or {}).get("points") or []
        color = native.get("color")
        if (isinstance(temperature, dict) and temperature.get("mirek_valid") is True
                and _number(temperature.get("mirek"), 153, 500)):
            colors = [_temperature(1_000_000 / temperature["mirek"])]
        elif isinstance(points, list) and 2 <= len(points) <= 5:
            colors = [_xy(point["color"]["xy"]) for point in points]
        elif isinstance(color, dict) and isinstance(color.get("xy"), dict):
            colors = [_xy(color["xy"])]
        else:
            colors = None
        brightness = (native.get("dimming") or {}).get("brightness")
        if not _number(brightness, 0, 100):
            brightness = None
        if colors is not None or brightness is not None:
            self._update(role, "scene-output", colors, brightness)

    def record_native_channels(self, channels):
        # These channels already include layout calibration and video intensity.
        count = sum(len(indices) for indices in self.native_channels.values())
        if isinstance(channels, (tuple, list)) and len(channels) == count:
            channels = dict(enumerate(channels))
        if (not isinstance(channels, dict)
                or any(type(index) is not int or not 0 <= index < count for index in channels)):
            raise ValueError('Invalid native preview channels')
        samples = {
            index: [round(value / 65535 * 255) for value in _rgb(rgb, 65535)]
            for index, rgb in channels.items()
        }
        with self.lock:
            for lamp, indices in self.native_channels.items():
                if all(index in samples for index in indices):
                    self._update(lamp, "sync-output", [samples[index] for index in indices], 100)

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.outputs)


def _hue_fresh(value):
    return (
        isinstance(value, dict)
        and value.get("healthy") is True
        and _number(value.get("native_age"), 0, 2)
        and value.get("native_mode") in ("normal", "streaming")
        and type(value.get("native_on")) is bool
    )


def build_preview(adapter_status, output_snapshot):
    """Project only known layout lamps from already assembled snapshots, without IO."""
    status = adapter_status if isinstance(adapter_status, dict) else {}
    output = output_snapshot if isinstance(output_snapshot, dict) else {}
    video = status.get("video")
    video = video if isinstance(video, dict) else {}
    hue = status.get("hue")
    hue = ({lamp: value for lamp, value in hue.items()
            if isinstance(lamp, str) and LIGHT_ID.match(lamp)} if isinstance(hue, dict) else {})
    # The adapter lists the registry lights; then exactly those are previewed, in
    # registry order, and one without a reading has an unknown state. Without the
    # list (an older adapter) the six migrated lamps come first.
    listed = status.get("lights")
    registry = {item["id"]: item.get("provider") for item in
                (listed if isinstance(listed, list) else [])
                if isinstance(item, dict) and isinstance(item.get("id"), str)
                and LIGHT_ID.match(item["id"])}
    if not isinstance(listed, list):
        for lamp in HUE_LAMPS:
            hue.setdefault(lamp, None)
    for lamp, provider in registry.items():
        if provider == "hue":
            hue.setdefault(lamp, None)
    targets = status.get("targets")
    targets = targets if isinstance(targets, list) else []
    govee = {
        item["light"]: item
        for item in targets
        if isinstance(item, dict) and isinstance(item.get("light"), str)
        and LIGHT_ID.match(item["light"]) and item["light"] not in hue
    }
    lamp_ids = (list(registry) if isinstance(listed, list)
                else list(LAMP_IDS) + sorted((set(govee) | set(hue)) - set(LAMP_IDS)))
    lamp_ids = lamp_ids[:MAX_LAMPS]
    composition = status.get('composition')
    included = set(lamp_ids)
    if composition is not None:
        selected = composition.get('enabled_roles') if isinstance(composition, dict) else None
        composed = composition.get('lights') if isinstance(composition, dict) else None
        included = ({ROLE_LAMPS[role] for role in selected}
                    if isinstance(selected, list) and len(selected) <= 6
                    and all(type(role) is str and role in ROLE_LAMPS for role in selected)
                    else set())
        # A light enrolled later is in Sync when its layout row is composed.
        if isinstance(composed, list):
            included |= {light for light in composed if type(light) is str
                         and light in lamp_ids and light not in LAMP_IDS}
    cleanup = video.get("cleanup_pending") is True or video.get("cleanupPending") is True
    sync = (
        video.get("active") is True
        and video.get("ready") is True
        and video.get("last_result", "streaming") == "streaming"
        and _number(video.get("frame_age"), 0, 0.5)
        and not cleanup
    )
    foreign = (
        any(lamp in included and _hue_fresh(value) and value["native_mode"] == "streaming"
            for lamp, value in hue.items())
        and video.get("native_owned") is not True
    )
    mode = (
        "waiting"
        if cleanup
        else "sync"
        if sync
        else "foreign"
        if foreign
        else "waiting"
        if any(video.get(field) is True for field in ("active", "ready", "native_owned"))
        else "scene"
    )
    lamps = {}
    for lamp in lamp_ids:
        brightness = None
        if lamp not in hue:
            native = _govee_reading(govee.get(lamp, {}))
            fresh = native is not None
            on = bool(native["onOff"]) if fresh else None
            brightness = native.get("brightness") if fresh else None
        else:
            item = hue.get(lamp)
            fresh = _hue_fresh(item)
            on = item["native_on"] if fresh else None
            brightness = item.get("native_brightness") if fresh else None
        sample = output.get(lamp)
        sample = sample if isinstance(sample, dict) else {}
        if _number(sample.get("brightness"), 0, 100):
            brightness = sample["brightness"]
        emitted_brightness = brightness if fresh and _number(brightness, 0, 100) else None
        brightness = round(emitted_brightness) if emitted_brightness is not None else None
        source = sample.get("source") if sample.get("source") in SOURCES else None
        expected = {"scene": "scene-output", "sync": "sync-output"}.get(mode)
        colors = []
        valid = False
        try:
            raw = sample.get("colors")
            if (
                source is not None
                and sample.get("color_source") == expected
                and expected
                and isinstance(raw, list)
                and 1 <= len(raw) <= 9
                and brightness is not None
            ):
                # Use the unrounded emitted gain. Public brightness is integer metadata.
                gain = emitted_brightness / 100
                colors = [[round(value * gain) for value in _rgb(color)] for color in raw]
                valid = True
        except (ValueError, TypeError):
            colors = []
        lamps[lamp] = {
            "online": True if fresh else None,
            "on": on,
            "brightness": brightness,
            "colors": colors if fresh and on and lamp in included else [],
            "source": source,
            "stale": not fresh or not valid or lamp not in included,
            "included": lamp in included,
        }
    return {"version": 1, "mode": mode, "lamps": lamps}
