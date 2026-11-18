"""Opt-in Signe capability profile without replacing existing virtual resources."""

import copy
import math

SIGNE_MODEL = "915005987201"


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
