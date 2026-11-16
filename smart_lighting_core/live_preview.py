"""Bounded, passive telemetry for emitted commands, not physical acknowledgement."""

import base64
import copy
import math
import threading
import time
from functools import reduce
from operator import xor

import smart_lighting as lan

HUE_ROLES = {
    "strip": "hue-monitor",
    "hue_left": "play-left",
    "hue_center": "play-center",
    "hue_right": "play-right",
}
LAMP_IDS = ("govee-left", "govee-right", *HUE_ROLES.values())
SOURCES = ("scene-output", "sync-output")


def _number(value, low, high):
    return type(value) in (int, float) and low <= value <= high and math.isfinite(value)


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


def _xy(value):
    x, y = value["x"], value["y"]
    if not _number(x, 0, 1) or not _number(y, 0.00001, 1):
        raise ValueError("Invalid output chromaticity")
    return list(lan.xy_to_rgb(x, y))


class OutputPreviewStore:
    """Share only successful emitted color/brightness state across output paths."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.RLock()
        self.outputs = {}

    def _update(self, lamp, source, colors=None, brightness=None):
        with self.lock:
            sample = self.outputs.setdefault(lamp, {})
            if colors is not None:
                if (
                    lamp in HUE_ROLES.values()
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
        side = device.get("side")
        if side not in ("left", "right") or not isinstance(data, dict):
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
        else:
            return
        self._update("govee-" + side, source, colors, brightness)

    def record_hue(self, role, body):
        if role not in HUE_ROLES or not isinstance(body, dict):
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
            self._update(HUE_ROLES[role], "scene-output", colors, brightness)

    def record_native_channels(self, channels):
        # These channels already include layout calibration and video intensity.
        samples = {
            index: [round(value / 65535 * 255) for value in _rgb(channels[index], 65535)]
            for index in range(12)
        }
        with self.lock:
            for lamp, indices in (
                ("play-center", [0]),
                ("play-left", [1]),
                ("play-right", [2]),
                ("hue-monitor", range(3, 12)),
            ):
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
    hue_play = status.get("hue_play")
    hue_play = hue_play if isinstance(hue_play, dict) else {}
    hue = {
        "hue-monitor": status.get("hue_strip"),
        **{lamp: hue_play.get(role) for role, lamp in HUE_ROLES.items() if role != "strip"},
    }
    cleanup = video.get("cleanup_pending") is True or video.get("cleanupPending") is True
    sync = (
        video.get("active") is True
        and video.get("ready") is True
        and video.get("last_result", "streaming") == "streaming"
        and _number(video.get("frame_age"), 0, 0.5)
        and not cleanup
    )
    foreign = (
        any(_hue_fresh(value) and value["native_mode"] == "streaming" for value in hue.values())
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
    targets = status.get("targets")
    targets = targets if isinstance(targets, list) else []
    govee = {
        item["side"]: item
        for item in targets
        if isinstance(item, dict) and item.get("side") in ("left", "right")
    }
    lamps = {}
    for lamp in LAMP_IDS:
        brightness = None
        if lamp.startswith("govee-"):
            item = govee.get(lamp.removeprefix("govee-"), {})
            native = item.get("native")
            native = native if isinstance(native, dict) else {}
            fresh = (
                native.get("identity_verified") is True
                and _number(item.get("status_age"), 0, 0.75)
                and type(native.get("onOff")) is int
                and native["onOff"] in (0, 1)
            )
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
            "colors": colors if fresh and on else [],
            "source": source,
            "stale": not fresh or not valid,
        }
    return {"version": 1, "mode": mode, "lamps": lamps}
