"""Per-lamp Govee calibration at the transport boundary.

Two H6076 lamps can render the same command differently from each other and
from the Hue lamps (see ``docs/design/2026-11-19-govee-device-calibration.md``).
The owner sets a Govee lamp's calibration in Light Studio: the layout row's
``calibration`` holds the slider positions ``warmth``, ``tint`` and
``lowLight``. ``curve()`` turns them into channel gains and a brightness
exponent (sent brightness is ``100 * (b / 100) ** exponent``). Everything above
the transport keeps logical values: commands are converted on the way out and
statuses on the way back. A calibration session may override a side's values.
"""

import base64
import copy
import threading
from functools import reduce
from operator import xor

from govee_segments import razer_colors, razer_data

import lights_can_communicate_v2 as lan


def curve(values):
    """Return (gains or None, exponent or None) for a row calibration."""
    w = values.get("warmth", 0) / 100
    t = values.get("tint", 0) / 100
    low = values.get("lowLight", 0) / 100
    red = green = blue = 1.0
    if w > 0:  # warmer: less blue, a little less green
        blue *= 1 - 0.6 * w
        green *= 1 - 0.18 * w
    elif w < 0:  # cooler: less red, a little less green
        red *= 1 + 0.6 * w
        green *= 1 + 0.18 * w
    if t < 0:  # greener: less red and blue
        red *= 1 + 0.3 * t
        blue *= 1 + 0.3 * t
    elif t > 0:  # pinker: less green
        green *= 1 - 0.3 * t
    peak = max(red, green, blue)
    gains = tuple(value / peak for value in (red, green, blue))
    return (None if gains == (1.0, 1.0, 1.0) else gains,
            None if low == 0 else 1 - 0.7 * low)


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
    def __init__(self):
        self.gains = self.exponent = None
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
    """Wraps the Govee sender and decodes statuses with each lamp's calibration."""

    def __init__(self, targets, layout_store=None):
        self.lock = threading.Lock()
        self.layout_store = layout_store
        self.sides, self.lamps, self.overrides = {}, {}, {}
        for item in targets:
            if "color_gains" in item or "brightness_exponent" in item:
                raise ValueError("Govee calibration belongs in the Light Studio layout, "
                                 "not in the target profile")
            key = lan.device_key(item)
            self.sides[key] = item.get("side")
            self.lamps[key] = _Lamp()

    def override(self, side, values):
        """Use ``values`` for ``side`` instead of the layout (None ends it)."""
        with self.lock:
            if values is None:
                self.overrides.pop(side, None)
            else:
                self.overrides[side] = dict(values)

    def _current(self, key):
        """The lamp with its present gains and exponent, or None if neutral."""
        lamp = self.lamps.get(key)
        if lamp is None:
            return None
        side = self.sides[key]
        values = self.overrides.get(side)
        if values is None and self.layout_store is not None:
            values = self.layout_store.calibration(side)
        lamp.gains, lamp.exponent = curve(values or {})
        if lamp.gains is None and lamp.exponent is None and lamp.brightness is None:
            return None
        return lamp

    def wrap(self, sender):
        def send(device, command, data):
            with self.lock:
                lamp = self._current(lan.device_key(device)) if isinstance(data, dict) else None
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
        result = dict(statuses)
        with self.lock:
            for key, value in statuses.items():
                if key not in self.lamps or not isinstance(value[0], dict):
                    continue
                lamp = self._current(key)
                if lamp is not None:
                    result[key] = (lamp.decode(value[0]), *value[1:])
        return result
