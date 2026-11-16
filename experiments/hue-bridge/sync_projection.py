"""Pure front-wall projection from saved layout geometry to capture RGB16 samples.

The capture palette is a stable screen perimeter, independent of fixture placement.
Physical brightness and calibration belong to output consumers, after projection.
"""

import copy
import hashlib
import json
import math

_ROLES = {
    "govee-left": "left",
    "govee-right": "right",
    "hue-monitor": "strip",
    "play-left": "hue_left",
    "play-center": "hue_center",
    "play-right": "hue_right",
}
_PLAY_CHANNELS = {"hue_left": 11, "hue_center": 12, "hue_right": 13}
# Clockwise perimeter distance from the bottom-left corner; coordinates have v up.
_PALETTE = (
    (0, 2), (0.5, 3), (1, 4), (1.25, 5), (1.5, 6), (1.75, 7),
    (2, 8), (2.5, 9), (3, 10), (3.25, 13), (3.5, 12), (3.75, 11), (4, 2),
)


def sync_profile(validated_layout):
    """Build a deterministic, public-role-only Sync profile from a validated layout."""
    devices = {row["id"]: row for row in validated_layout["devices"] if row["id"] in _ROLES}
    monitor = devices["hue-monitor"]["positionCm"]
    width = validated_layout["monitorInches"] * 2.54 * 16 / math.hypot(16, 9)
    height = width * 9 / 16
    desk_height = validated_layout["desk"]["height"]

    def screen_point(x, physical_height):
        u = max(0, min(1, (x - monitor["x"]) / width + 0.5))
        v = max(0, min(1, (physical_height - monitor["height"]) / height + 0.5))
        # Clamping handles outside points, including corners. For interior points,
        # choose the nearest normalized screen edge, with stable tie handling.
        edge = min(range(4), key=(u, 1 - u, v, 1 - v).__getitem__)
        return ((0, v), (1, v), (u, 0), (u, 1))[edge]

    enabled = [role for key, role in _ROLES.items() if devices[key].get("enabled", True)]
    profile = {
        "basis": "screen-perimeter-v1",
        "enabled_roles": enabled,
        "native_roles": [role for role in enabled if role not in ("left", "right")],
        "govee": {},
        "plays": {},
    }
    for key, side in (("govee-left", "left"), ("govee-right", "right")):
        position = devices[key]["positionCm"]
        base = position["height"] - 60
        samples = []
        for segment in range(7):
            low = base + 6 + segment * 128 / 7
            high = base + 6 + (segment + 1) * 128 / 7
            visible_low = max(low, desk_height)
            samples.append(
                screen_point(position["x"], (visible_low + high) / 2)
                if high > visible_low else None
            )
        profile["govee"][side] = {
            "samples": samples,
            "visible": [sample is not None for sample in samples],
        }
    for key, role in _ROLES.items():
        if role in _PLAY_CHANNELS:
            position = devices[key]["positionCm"]
            profile["plays"][role] = screen_point(
                position["x"], max(position["height"], desk_height)
            )
    encoded = json.dumps(profile, sort_keys=True, separators=(",", ":"), allow_nan=False)
    profile["signature"] = hashlib.sha256(encoded.encode()).hexdigest()
    return profile


def _validate_frame(frame):
    if not isinstance(frame, dict) or not set(range(2, 14)) <= frame.keys():
        raise ValueError("Expected a capture frame with all screen-perimeter channels")
    for rgb in frame.values():
        if (not isinstance(rgb, (tuple, list)) or len(rgb) != 3
                or any(type(value) is not int or not 0 <= value <= 65535 for value in rgb)):
            raise ValueError("Expected integer RGB16 capture colors")


def _sample(frame, point):
    u, v = point
    if u == 0:
        distance = v
    elif v == 1:
        distance = 1 + u
    elif u == 1:
        distance = 3 - v
    else:
        distance = 4 - u
    for (start, first), (end, last) in zip(_PALETTE, _PALETTE[1:]):
        if start <= distance <= end:
            fraction = (distance - start) / (end - start)
            return tuple(round(a + (b - a) * fraction)
                         for a, b in zip(frame[first], frame[last]))
    raise ValueError("Expected a screen-perimeter sample")


def project_native_frame(frame, profile):
    """Project enabled physical Play colors from the original capture palette."""
    _validate_frame(frame)
    result = copy.deepcopy(frame)
    for role, channel in _PLAY_CHANNELS.items():
        if role in profile["native_roles"]:
            result[channel] = _sample(frame, profile["plays"][role])
    return result


def project_govee_frame(frame, side, profile):
    """Return seven bottom-to-top RGB16 segments, black where hidden or excluded."""
    _validate_frame(frame)
    if side not in ("left", "right"):
        raise ValueError("Expected a left or right floor-lamp role")
    if side not in profile["enabled_roles"]:
        return [(0, 0, 0)] * 7
    return [_sample(frame, point) if point is not None else (0, 0, 0)
            for point in profile["govee"][side]["samples"]]
