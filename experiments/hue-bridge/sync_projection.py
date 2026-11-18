"""Pure front-wall projection from saved layout geometry to capture RGB16 samples.

The capture palette is a stable screen perimeter, independent of fixture placement.
Physical brightness and calibration belong to output consumers, after projection.
"""

import copy
import hashlib
import json
import math

from sync_zone import vertical_bounds, zone_points

_ROLES = {
    "govee-left": "left",
    "govee-right": "right",
    "hue-monitor": "strip",
    "play-left": "hue_left",
    "play-center": "hue_center",
    "play-right": "hue_right",
}
_PLAY_CHANNELS = {"hue_left": 11, "hue_center": 12, "hue_right": 13}
_DESK_CENTER_CM = 100
_EDGE_FADE_CM = 15
# Clockwise perimeter distance from the bottom-left corner; coordinates have v up.
_PALETTE = (
    (0, 2), (0.5, 3), (1, 4), (1.25, 5), (1.5, 6), (1.75, 7),
    (2, 8), (2.5, 9), (3, 10), (3.25, 13), (3.5, 12), (3.75, 11), (4, 2),
)


def _point_gain(height, desk_height, fade):
    t = max(0, min(1, (height - desk_height + fade) / fade))
    return t * t * (3 - 2 * t)


def _segment_visibility(low, high, desk_height, fade):
    """Average the smooth penumbra over a segment and find its lit centroid."""
    if fade <= 1e-9:
        start = max(low, desk_height)
        return ((high-start)/(high-low), (start+high)/2) if high > start else (0,None)
    lower = desk_height - fade
    if high <= lower:
        return 0, None
    if low >= desk_height:
        return 1, (low + high) / 2

    def integral(height):
        if height <= lower:
            return 0, 0
        if height >= desk_height:
            return (fade / 2 + height - desk_height,
                    lower * fade / 2 + .35 * fade ** 2
                    + (height ** 2 - desk_height ** 2) / 2)
        t = (height - lower) / fade
        area = fade * (t ** 3 - .5 * t ** 4)
        moment = lower * area + fade ** 2 * (.75 * t ** 4 - .4 * t ** 5)
        return area, moment

    a0, m0 = integral(low)
    a1, m1 = integral(high)
    area = a1 - a0
    if area <= 0:
        return 0, None
    return (max(0, min(1, area / (high - low))),
            max(low, min(high, (m1 - m0) / area)))


def _attenuate(rgb, gain):
    if type(gain) not in (int, float) or not math.isfinite(gain) or not 0 <= gain <= 1:
        raise ValueError('Invalid Sync visibility gain')
    return tuple(round(value * gain) for value in rgb)


def _visibility_function(zone, fade_on):
    """Return gain(x, height) for a resolved zone; every gain is 1 while the fade is off."""
    def visibility(x, physical_height):
        if not fade_on:
            return 1
        bounds = vertical_bounds(zone, x)
        if bounds is None:
            return 1
        lower, upper = bounds
        return (_point_gain(physical_height, upper, upper-lower) if upper > lower
                else int(physical_height >= upper))
    return visibility


def _govee_segments(zone, fade_on, x, base):
    """Return per-segment (gain, lit centre height) from bottom to top, plus the fade width."""
    bounds = vertical_bounds(zone, x) if fade_on else None
    fade = bounds[1]-bounds[0] if bounds is not None else 0
    rows = []
    for segment in range(7):
        low = base + 6 + segment * 128 / 7
        high = base + 6 + (segment + 1) * 128 / 7
        rows.append(_segment_visibility(low, high, bounds[1], fade)
                    if bounds is not None else (1, (low+high)/2))
    return rows, fade


def _devices(validated_layout):
    return {row["id"]: row for row in validated_layout["devices"] if row["id"] in _ROLES}


def _strip_gains(visibility, monitor, width, height):
    anchors = [
        (-.5,-.5),(-.5,0),(-.5,.5),(-.25,.5),(0,.5),(.25,.5),(.5,.5),(.5,0),(.5,-.5),
    ]
    return [visibility(monitor['x']+u*width, monitor['height']+v*height) for u,v in anchors]


def sync_profile(validated_layout):
    """Build a deterministic, public-role-only Sync profile from a validated layout."""
    devices = _devices(validated_layout)
    monitor = devices["hue-monitor"]["positionCm"]
    width = validated_layout["monitorInches"] * 2.54 * 16 / math.hypot(16, 9)
    height = width * 9 / 16
    zone = zone_points(validated_layout)
    fade_on = validated_layout.get("syncFalloff", True)
    visibility = _visibility_function(zone, fade_on)

    def screen_point(x, physical_height):
        u = max(0, min(1, (x - monitor["x"]) / width + 0.5))
        v = max(0, min(1, (physical_height - monitor["height"]) / height + 0.5))
        # Clamping handles outside points, including corners. For interior points,
        # choose the nearest normalized screen edge, with stable tie handling.
        edge = min(range(4), key=(u, 1 - u, v, 1 - v).__getitem__)
        return ((0, v), (1, v), (u, 0), (u, 1))[edge]

    # Removed rows and rows that keep their own light are outside the composition.
    enabled = [role for key, role in _ROLES.items()
               if devices[key].get("enabled", True)
               and devices[key].get("syncRole", "sync") != "own"]
    profile = {
        "basis": "screen-perimeter-v1",
        "enabled_roles": enabled,
        "native_roles": [role for role in enabled if role not in ("left", "right")],
        "govee": {},
        "plays": {},
        "play_gains": {},
        'zone': zone,
    }
    for key, side in (("govee-left", "left"), ("govee-right", "right")):
        position = devices[key]["positionCm"]
        rows, fade = _govee_segments(zone, fade_on, position['x'], position["height"] - 60)
        samples = [screen_point(position['x'], center) if center is not None else None
                   for _, center in rows]
        profile["govee"][side] = {
            "samples": samples,
            "visible": [sample is not None for sample in samples],
            'gains': [gain for gain, _ in rows],
            'fade_cm': fade,
        }
    for key, role in _ROLES.items():
        if role in _PLAY_CHANNELS:
            position = devices[key]["positionCm"]
            profile["plays"][role] = screen_point(position['x'], position['height'])
            profile['play_gains'][role] = visibility(position['x'], position['height'])
    profile['strip_gains'] = _strip_gains(visibility, monitor, width, height)
    encoded = json.dumps(profile, sort_keys=True, separators=(",", ":"), allow_nan=False)
    profile["signature"] = hashlib.sha256(encoded.encode()).hexdigest()
    return profile


def scene_fade(validated_layout):
    """Return the without-Sync fade gains per role from ``sceneZone``/``sceneFalloff``.

    ``left``/``right`` hold seven Govee segment gains from bottom to top, ``strip`` the
    mean of the nine monitor-strip anchor gains and ``hue_*`` one gain per Play. Every
    gain is 1 while the scene fade is off (the default).
    """
    devices = _devices(validated_layout)
    zone = zone_points(validated_layout, "sceneZone")
    fade_on = validated_layout.get("sceneFalloff", False)
    visibility = _visibility_function(zone, fade_on)
    monitor = devices["hue-monitor"]["positionCm"]
    width = validated_layout["monitorInches"] * 2.54 * 16 / math.hypot(16, 9)
    result = {}
    for key, side in (("govee-left", "left"), ("govee-right", "right")):
        position = devices[key]["positionCm"]
        rows, _ = _govee_segments(zone, fade_on, position["x"], position["height"] - 60)
        result[side] = [gain for gain, _ in rows]
    strip = _strip_gains(visibility, monitor, width, width * 9 / 16)
    result["strip"] = sum(strip) / len(strip)
    for key, role in _ROLES.items():
        if role in _PLAY_CHANNELS:
            position = devices[key]["positionCm"]
            result[role] = visibility(position["x"], position["height"])
    return result


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
    if 'strip' in profile['native_roles'] and 'strip_gains' in profile:
        if len(profile['strip_gains']) != 9:
            raise ValueError('Expected nine monitor-strip visibility gains')
        for index,gain in enumerate(profile['strip_gains'],2):
            result[index] = _attenuate(frame[index], gain)
    for role, channel in _PLAY_CHANNELS.items():
        if role in profile["native_roles"]:
            result[channel] = _attenuate(_sample(frame, profile["plays"][role]),
                                         profile.get('play_gains', {}).get(role, 1))
    return result


def project_govee_frame(frame, side, profile):
    """Return seven bottom-to-top RGB16 segments, black where hidden or excluded."""
    _validate_frame(frame)
    if side not in ("left", "right"):
        raise ValueError("Expected a left or right floor-lamp role")
    if side not in profile["enabled_roles"]:
        return [(0, 0, 0)] * 7
    row = profile['govee'][side]
    gains = row.get('gains', [1] * 7)
    if len(row['samples']) != 7 or len(gains) != 7:
        raise ValueError('Expected seven projected segments')
    return [_attenuate(_sample(frame, point), gain) if point is not None else (0, 0, 0)
            for point, gain in zip(row['samples'], gains)]
