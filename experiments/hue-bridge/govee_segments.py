"""H6076 LAN segment colors, ordered from bottom to top.

Protocol reference: Galorhallen/govee-local-api, SegmentColorMessages and
SEGMENT_CODES at commit 886fc569168be474d727c80ba56ade4d7237a58b.
No power commands are included. DreamView ("razer") frames follow the OpenRGB
Govee LAN controller: BB 00 <size> <command> <data> <xor>, base64 in "pt".
"""

import base64
from functools import reduce
from operator import xor

SEGMENTS = 7


def _colors(values):
    if not isinstance(values, (list, tuple)) or not 1 <= len(values) <= 16:
        raise ValueError("Expected one to sixteen RGB color stops")
    result = []
    for color in values:
        if (
            not isinstance(color, (list, tuple))
            or len(color) != 3
            or any(type(value) is not int or not 0 <= value <= 255 for value in color)
        ):
            raise ValueError("Expected integer RGB channels in 0..255")
        result.append(tuple(color))
    return result


def interpolate(stops, reverse=False):
    colors = _colors(stops)
    result = []
    for index in range(SEGMENTS):
        position = index * (len(colors) - 1) / (SEGMENTS - 1)
        first = int(position)
        last = min(first + 1, len(colors) - 1)
        fraction = position - first
        result.append(
            tuple(round(a + (b - a) * fraction) for a, b in zip(colors[first], colors[last]))
        )
    return list(reversed(result)) if reverse else result


def segment_data(colors):
    colors = _colors(colors)
    if len(colors) != SEGMENTS:
        raise ValueError("H6076 output requires exactly seven segment colors")
    commands = []
    for index, color in enumerate(colors):
        payload = bytes([0x33, 0x05, 0x15, 0x01, *color])
        payload += bytes(5) + (1 << index).to_bytes(2, "little") + bytes(5)
        frame = payload + bytes([reduce(xor, payload, 0)])
        commands.append(base64.b64encode(frame).decode("ascii"))
    return {"command": commands}


RAZER_MODE = 0xB1
RAZER_COLORS = 0xB0


def _razer(command, data):
    body = bytes([0xBB, 0x00, len(data), command, *data])
    return {"pt": base64.b64encode(body + bytes([reduce(xor, body, 0)])).decode("ascii")}


def razer_mode(enabled):
    """Switch DreamView streaming; the lamp leaves it after a minute without frames."""
    return _razer(RAZER_MODE, [1 if enabled else 0])


def razer_data(colors):
    """One streaming frame: the lamp blends the colors from bottom to top."""
    colors = _colors(colors)
    return _razer(RAZER_COLORS, [1, len(colors), *(value for color in colors for value in color)])


def razer_colors(data):
    """Colors of a razer frame, or None for any other razer packet."""
    raw = base64.b64decode(data["pt"], validate=True)
    if (len(raw) < 6 or raw[:2] != b"\xbb\x00" or raw[2] != len(raw) - 5
            or reduce(xor, raw[:-1], 0) != raw[-1]):
        raise ValueError("Invalid razer packet")
    if raw[3] != RAZER_COLORS:
        return None
    count = raw[5]
    values = raw[6:-1]
    if len(values) != count * 3 or not 1 <= count <= 16:
        raise ValueError("Invalid razer colors")
    return [list(values[index:index + 3]) for index in range(0, len(values), 3)]
