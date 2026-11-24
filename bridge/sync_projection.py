"""Pure front-wall projection from saved layout geometry to capture RGB16 samples.

The capture palette is a stable screen perimeter, independent of fixture placement.
Physical brightness and calibration belong to output consumers, after projection.
"""

import copy
import hashlib
import json
import math

import light_geometry
from desk_layout import composed, screen_size
from light_geometry import geometry_for, segment_spans
from sync_zone import vertical_bounds, zone_points

# The migrated rows; the two Govee lamps keep their side names in ``enabled_roles``.
_ROLES = {
    "govee-left": "left",
    "govee-right": "right",
    "hue-monitor": "hue-monitor",
    "play-left": "play-left",
    "play-center": "play-center",
    "play-right": "play-right",
}
# Capture channel of each migrated Play light (see entertainment_stream.NATIVE_SOURCES).
_PLAY_CHANNELS = {"play-left": 11, "play-center": 12, "play-right": 13}
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


def _devices(validated_layout):
    return {row["id"]: row for row in validated_layout["devices"] if row["id"] in _ROLES}


class _Projector:
    """Per-light zone samples and visibility gains for one zone and fade switch."""

    def __init__(self, validated_layout, zone_key="syncZone", fade_key="syncFalloff",
                 fade_default=True):
        self.screen = validated_layout["screen"]
        center = self.screen["positionCm"]
        width, height = screen_size(validated_layout)
        self.zone = zone_points(validated_layout, zone_key)
        self.fade_on = validated_layout.get(fade_key, fade_default)
        self.visibility = _visibility_function(self.zone, self.fade_on)

        def screen_point(x, physical_height):
            u = max(0, min(1, (x - center["x"]) / width + 0.5))
            v = max(0, min(1, (physical_height - center["height"]) / height + 0.5))
            # Clamping handles outside points, including corners. For interior points,
            # choose the nearest normalized screen edge, with stable tie handling.
            edge = min(range(4), key=(u, 1 - u, v, 1 - v).__getitem__)
            return ((0, v), (1, v), (u, 0), (u, 1))[edge]

        self.screen_point = screen_point

    def light(self, row, zones=1):
        """Return ``samples`` (None where hidden) and ``gains`` of a row.

        A segmented floor lamp also gets ``fade_cm``, the width of its fade band.
        A point light gets ``zones`` zones (the channels of a native Hue light).
        """
        kind = geometry_for(row)["kind"]
        x = row["positionCm"]["x"]
        if kind == "segmented-floor":
            bounds = vertical_bounds(self.zone, x) if self.fade_on else None
            fade = bounds[1] - bounds[0] if bounds is not None else 0
            # The lit part of each segment, so a faded segment samples where it is lit.
            zones = [_segment_visibility(low, high, bounds[1], fade) if bounds is not None
                     else (1, (low + high) / 2) for low, high in segment_spans(row)]
            gains = [gain for gain, _ in zones]
            samples = [self.screen_point(x, center) if center is not None else None
                       for _, center in zones]
            return {"samples": samples, "gains": gains, "fade_cm": fade}
        points = light_geometry.zone_points(row, self.screen, zones)
        gains = [self.visibility(px, ph) for px, ph in points]
        samples = [self.screen_point(px, ph) for px, ph in points]
        if kind == "screen":
            # Its anchors lie on the capture channels; drop the rounding noise of
            # the round trip through centimeters so they hit those channels exactly.
            samples = [tuple(round(value, 9) for value in point) for point in samples]
        return {"samples": samples, "gains": gains}


def light_entry(row, validated_layout):
    """Return the Sync zone samples and gains of one layout row, whatever its id."""
    return _Projector(validated_layout).light(row)


def _light_entries(validated_layout, projector=None, **options):
    devices = _devices(validated_layout)
    projector = projector or _Projector(validated_layout, **options)
    # A migrated light without a layout row has no entry.
    return {key: projector.light(devices[key]) for key in _ROLES if key in devices}


def sync_profile(validated_layout, native=None, migrated=None):
    """Build a deterministic, public-role-only Sync profile from a validated layout.

    ``native`` maps each enrolled native Hue light outside the migrated rows to its
    channel count. Such a light is driven by the native relay with one projected
    zone per channel. ``migrated`` is the set of migrated Hue lights the registry
    drives (None: all four); the row of a disconnected one is not a native role,
    so Sync does not wait for a light that has no output.
    """
    native = native or {}
    devices = _devices(validated_layout)
    projector = _Projector(validated_layout)
    entries = _light_entries(validated_layout, projector)
    # Removed rows and rows with their own light or off during Sync are outside it.
    members = [key for key in _ROLES if key in devices and composed(devices[key])]
    enabled = [_ROLES[key] for key in members]
    # Every composed light gets an entry; layout-only drawings never produce output.
    lights = {row["id"]: entries[row["id"]] if row["id"] in entries
              else projector.light(row, native.get(row["id"], 1))
              for row in validated_layout["devices"]
              if composed(row) and not row["id"].startswith("virtual-")}
    profile = {
        "basis": "screen-perimeter-v1",
        "enabled_roles": enabled,
        "native_roles": [role for role in enabled if role not in ("left", "right")
                         and (migrated is None or role in migrated)]
        + [light for light in lights if light in native],
        "lights": lights,
        # The legacy keys below are derived from the per-light entries of the role rows
        # that the layout holds.
        "govee": {},
        "plays": {},
        "play_gains": {},
        "zone": zone_points(validated_layout),
    }
    for key, side in (("govee-left", "left"), ("govee-right", "right")):
        if key not in entries:
            continue
        entry = entries[key]
        profile["govee"][side] = {
            "samples": entry["samples"],
            "visible": [sample is not None for sample in entry["samples"]],
            "gains": entry["gains"],
            "fade_cm": entry["fade_cm"],
        }
    for key, role in _ROLES.items():
        if role in _PLAY_CHANNELS and key in entries:
            profile["plays"][role] = entries[key]["samples"][0]
            profile["play_gains"][role] = entries[key]["gains"][0]
    if "hue-monitor" in entries:
        profile["strip_gains"] = entries["hue-monitor"]["gains"]
    encoded = json.dumps(profile, sort_keys=True, separators=(",", ":"), allow_nan=False)
    profile["signature"] = hashlib.sha256(encoded.encode()).hexdigest()
    return profile


def scene_fade(validated_layout):
    """Return the without-Sync fade gains per light from ``sceneZone``/``sceneFalloff``.

    ``govee-left`` and ``govee-right`` hold seven Govee segment gains from bottom to
    top, ``hue-monitor`` the mean of the nine monitor-strip anchor gains and
    ``play-*`` one gain per Play. Every gain is 1 while the scene fade is off (the
    default). Scenes reach other Govee lights only with the virtual bridge stage, so
    they have no entry yet.
    """
    entries = _light_entries(validated_layout, zone_key="sceneZone", fade_key="sceneFalloff",
                             fade_default=False)
    result = {key: entries[key]["gains"] for key in ("govee-left", "govee-right")
              if key in entries}
    if "hue-monitor" in entries:
        strip = entries["hue-monitor"]["gains"]
        result["hue-monitor"] = sum(strip) / len(strip)
    for key, role in _ROLES.items():
        if role in _PLAY_CHANNELS and key in entries:
            result[role] = entries[key]["gains"][0]
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


def _project_zones(frame, samples, gains):
    """Return the attenuated sample of every zone, black where the zone is hidden."""
    if len(samples) != len(gains):
        raise ValueError("Expected one visibility gain per projected zone")
    return [_attenuate(_sample(frame, point), gain) if point is not None else (0, 0, 0)
            for point, gain in zip(samples, gains)]


def project_light_frame(frame, light_id, profile):
    """Return the projected RGB16 colors of every zone of a composed light."""
    _validate_frame(frame)
    entry = profile.get("lights", {}).get(light_id)
    if entry is None:
        raise ValueError("Expected a light that takes part in the Sync composition")
    return _project_zones(frame, entry["samples"], entry["gains"])


def project_native_frame(frame, profile):
    """Project enabled physical Play colors from the original capture palette."""
    _validate_frame(frame)
    result = copy.deepcopy(frame)
    if 'hue-monitor' in profile['native_roles'] and 'strip_gains' in profile:
        if len(profile['strip_gains']) != 9:
            raise ValueError('Expected nine monitor-strip visibility gains')
        # The strip's nine zones sit on capture channels 2-10, so each keeps its own
        # channel and only the visibility gain applies.
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
    return _project_zones(frame, row['samples'], gains)
