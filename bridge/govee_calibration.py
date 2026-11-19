"""Per-lamp Govee calibration at the transport boundary.

Two H6076 lamps can render the same command differently: one may dim lower
levels more and show a weaker green (see
``docs/design/2026-11-19-govee-device-calibration.md``). A target profile may
carry ``color_gains`` (red, green, blue; each 0 < gain <= 1) and
``brightness_exponent`` (0 < exponent <= 1; sent brightness is
``100 * (b / 100) ** exponent``). Everything above the transport keeps logical
values: commands are converted on the way out and statuses on the way back.
"""

import base64
import copy
import math
import threading
from functools import reduce
from operator import xor

from govee_segments import razer_colors, razer_data

import lights_can_communicate_v2 as lan


def _gains(value):
    if value is None:
        return None
    if (not isinstance(value, list) or len(value) != 3
            or any(type(gain) not in (int, float) or not math.isfinite(gain)
                   or not 0 < gain <= 1 for gain in value)):
        raise ValueError("Invalid Govee color_gains")
    return tuple(float(gain) for gain in value)


def _exponent(value):
    if value is None:
        return None
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 1:
        raise ValueError("Invalid Govee brightness_exponent")
    return float(value)


def _scaled(color, gains):
    """Apply the gains and keep the colour's peak channel."""
    raw = [value * gain for value, gain in zip(color, gains)]
    peak = max(raw)
    if peak == 0:
        return [0, 0, 0]
    factor = max(color) / peak
    return [min(255, round(value * factor)) for value in raw]


def _unscaled(color, gains):
    return _scaled(color, [1 / gain for gain in gains])


def _ptreal(data, gains):
    commands = []
    for item in data["command"]:
        frame = bytearray(base64.b64decode(item, validate=True))
        if len(frame) != 20 or frame[:4] != b"\x33\x05\x15\x01":
            return data
        frame[4:7] = bytes(_scaled(list(frame[4:7]), gains))
        frame[-1] = reduce(xor, frame[:-1], 0)
        commands.append(base64.b64encode(bytes(frame)).decode("ascii"))
    return {**data, "command": commands}


class _Lamp:
    def __init__(self, gains, exponent):
        self.gains, self.exponent = gains, exponent
        # Logical value -> device value last sent. Several logical values can
        # reach the lamp as one device value; the one sent here reads back.
        self.brightness = None
        self.color = None

    def level(self, value):
        if self.exponent is None or value == 0:
            return value
        return max(1, min(100, round(100 * (value / 100) ** self.exponent)))

    def encode(self, command, data):
        if command == "brightness" and self.exponent is not None:
            sent = {**data, "value": self.level(data["value"])}
            return sent, ("brightness", data["value"], sent["value"])
        if self.gains is None:
            return data, None
        if command == "razer":
            colors = razer_colors(data)
            if colors is None:
                return data, None
            return razer_data([_scaled(color, self.gains) for color in colors]), None
        if command == "ptReal":
            return _ptreal(data, self.gains), None
        if command == "colorwc" and data.get("colorTemInKelvin") == 0:
            color = [data["color"][channel] for channel in "rgb"]
            sent = _scaled(color, self.gains)
            return ({**data, "color": dict(zip("rgb", sent))},
                    ("color", color, sent))
        return data, None

    def decode(self, status):
        result = copy.deepcopy(status)
        value = status.get("brightness")
        if self.exponent is not None and type(value) is int:
            if self.brightness is not None and self.brightness[1] == value:
                result["brightness"] = self.brightness[0]
            else:
                # A lit level never reads back as 0.
                level = round(100 * (value / 100) ** (1 / self.exponent))
                result["brightness"] = max(1, level) if value else 0
        color = status.get("color")
        if (self.gains is not None and isinstance(color, dict)
                and status.get("colorTemInKelvin") == 0
                and all(type(color.get(channel)) is int for channel in "rgb")):
            raw = [color[channel] for channel in "rgb"]
            if self.color is not None and self.color[1] == raw:
                logical = self.color[0]
            else:
                logical = _unscaled(raw, self.gains)
            result["color"] = dict(zip("rgb", logical))
        return result


class GoveeCalibration:
    """Validates the target profiles; wraps the sender and decodes statuses."""

    def __init__(self, targets):
        self.lock = threading.Lock()
        self.lamps = {}
        for item in targets:
            gains = _gains(item.get("color_gains"))
            exponent = _exponent(item.get("brightness_exponent"))
            if gains is not None or exponent is not None:
                self.lamps[lan.device_key(item)] = _Lamp(gains, exponent)

    def wrap(self, sender):
        def send(device, command, data):
            lamp = self.lamps.get(lan.device_key(device)) if isinstance(data, dict) else None
            if lamp is None:
                return sender(device, command, data)
            sent, memory = lamp.encode(command, data)
            result = sender(device, command, sent)
            if result and memory is not None:
                with self.lock:
                    setattr(lamp, memory[0], (memory[1], memory[2]))
            return result

        return send

    def decode(self, statuses):
        """Return the statuses with calibrated lamps in logical values."""
        if not self.lamps:
            return statuses
        result = dict(statuses)
        with self.lock:
            for key, value in statuses.items():
                lamp = self.lamps.get(key)
                if lamp is not None and isinstance(value[0], dict):
                    result[key] = (lamp.decode(value[0]), *value[1:])
        return result
