"""H6076 LAN segment colors, ordered from bottom to top.

Protocol reference: Galorhallen/govee-local-api, SegmentColorMessages and
SEGMENT_CODES at commit 886fc569168be474d727c80ba56ade4d7237a58b.
No power or mode-switch commands are included.
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
