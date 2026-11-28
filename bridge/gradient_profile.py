"""Opt-in Signe capability profile without replacing existing virtual resources."""

import colorsys
import copy
import math

SIGNE_MODEL = "915005987201"
# The gateway's gradient lightstrip model; it stands for every native Hue gradient light.
HUE_GRADIENT_MODEL = "LCX006"


def validate_gradient(value):
    if not isinstance(value, dict) or not isinstance(value.get("points"), list):
        raise ValueError("Expected gradient points")
    # Hue accepts up to five points for any gradient light, whatever its
    # points_capable; the Hue app relies on that in scenes. The adapter spreads
    # one to five points over the physical zones.
    if not 1 <= len(value["points"]) <= 5:
        raise ValueError("A gradient has one to five points")
    for point in value["points"]:
        if not isinstance(point, dict) or not isinstance(point.get("color"), dict):
            raise ValueError("Expected gradient color")
        xy = point["color"].get("xy")
        if not isinstance(xy, dict):
            raise ValueError("Expected gradient xy")
        x, y = xy.get("x"), xy.get("y")
        if (
            type(x) not in (int, float)
            or type(y) not in (int, float)
            or not math.isfinite(x)
            or not math.isfinite(y)
            or not 0 <= x <= 1
            or not 0 < y <= 1
            or x + y > 1.0001
        ):
            raise ValueError("Invalid gradient chromaticity")


def _rgb_xy(rgb):
    """CIE xy of an sRGB colour with channels 0-1."""
    linear = [((c + 0.055) / 1.055) ** 2.4 if c > 0.04045 else c / 12.92 for c in rgb]
    x = 0.4124 * linear[0] + 0.3576 * linear[1] + 0.1805 * linear[2]
    y = 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]
    z = 0.0193 * linear[0] + 0.1192 * linear[1] + 0.9505 * linear[2]
    return x / (x + y + z), y / (x + y + z)


def _kelvin_xy(kelvin):
    """CIE xy of the Planckian locus (Kim et al. cubic spline, 1667-25000 K)."""
    t = min(25000, max(1667, kelvin))
    if t <= 4000:
        x = -0.2661239e9 / t**3 - 0.2343589e6 / t**2 + 0.8776956e3 / t + 0.179910
    else:
        x = -3.0258469e9 / t**3 + 2.1070379e6 / t**2 + 0.2226347e3 / t + 0.240390
    if t <= 2222:
        y = -1.1063814 * x**3 - 1.34811020 * x**2 + 2.18555832 * x - 0.20219683
    elif t <= 4000:
        y = -0.9549476 * x**3 - 1.37418593 * x**2 + 2.09137015 * x - 0.16748867
    else:
        y = 3.0817580 * x**3 - 5.87338670 * x**2 + 3.75112997 * x - 0.37001483
    return x, y


def _active_xy(state):
    """CIE xy of the colour the light shows now, in its active colour mode."""
    try:
        mode = state.get("colormode", "xy")
        if mode == "ct":
            return _kelvin_xy(1_000_000 / min(1000, max(100, float(state["ct"]))))
        if mode == "hs":
            hue = min(65535, max(0, float(state["hue"]))) / 65535
            sat = min(254, max(0, float(state["sat"]))) / 254
            return _rgb_xy(colorsys.hsv_to_rgb(hue, sat, 1))
        x, y = state["xy"]
        return float(x), float(y)
    except (KeyError, TypeError, ValueError):
        return 0.3127, 0.3290


def configure_hue_gradient(lamp):
    """Make the virtual light of a native Hue gradient light a gradient light.

    An earlier gateway made a color bulb for a gradient light outside the
    Entertainment area. The light changes in place, so its ids, name, room and
    scenes stay. Its current color stays the output until the next change, and
    every section starts with that color.
    """
    if lamp.modelid == HUE_GRADIENT_MODEL:
        return
    lamp.modelid = HUE_GRADIENT_MODEL
    # An archetype the owner chose in the Hue app stays.
    if lamp.config.get("archetype") == "sultanbulb":
        lamp.config["archetype"] = "huelightstrip"
    lamp.protocol_cfg["points_capable"] = 5
    kind = lamp.state.get("colormode", "xy")
    lamp.protocol_cfg["proxy_color_kind"] = kind if kind in {"ct", "hs"} else "xy"
    x, y = (round(value, 4) for value in _active_xy(lamp.state))
    lamp.state["gradient"] = {"points": [{"color": {"xy": {"x": x, "y": y}}} for _ in range(5)]}


def configure_signe(lamp, enabled):
    if enabled:
        migrating = lamp.modelid != SIGNE_MODEL
        if migrating:
            lamp.protocol_cfg["single_color_profile"] = {
                "modelid": lamp.modelid,
                "config": copy.deepcopy(lamp.config),
                "function": lamp.function,
            }
        lamp.modelid = SIGNE_MODEL
        lamp.config.update(archetype="huesigne", function="decorative", direction="horizontal")
        lamp.function = "decorative"
        lamp.protocol_cfg["points_capable"] = 3
        gradient = lamp.state.get("gradient")
        if migrating or not isinstance(gradient, dict) or not gradient.get("points"):
            x, y = lamp.state.get("xy", [0.3127, 0.3290])
            point = {"color": {"xy": {"x": x, "y": y}}}
            lamp.state["gradient"] = {"points": [copy.deepcopy(point) for _ in range(3)]}
        kind = lamp.state.get("colormode", "xy")
        if migrating:
            lamp.protocol_cfg["proxy_color_kind"] = kind if kind in {"ct", "hs"} else "xy"
        else:
            lamp.protocol_cfg.setdefault("proxy_color_kind", "ct" if kind == "ct" else "gradient")
    elif lamp.modelid == SIGNE_MODEL:
        original = lamp.protocol_cfg.pop("single_color_profile", {})
        lamp.modelid = original.get("modelid", "LCT015")
        lamp.config = original.get(
            "config",
            {
                "archetype": "sultanbulb",
                "function": "mixed",
                "direction": "omnidirectional",
            },
        )
        lamp.function = original.get("function", "mixed")
        lamp.protocol_cfg.pop("points_capable", None)
        lamp.protocol_cfg.pop("proxy_color_kind", None)
