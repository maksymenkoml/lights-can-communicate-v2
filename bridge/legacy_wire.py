"""Translate the side-keyed wire format of ``/power-owner``.

The obsolete Homebridge overlay (``/power-owner``) still names the two migrated
lamps ``left`` and ``right``. Everything inside the adapter uses registry light ids,
so this route translates at the HTTP boundary and only for the two legacy lights. A
Govee light without a legacy side is not visible there.
"""

from lights_can_communicate_v2_core.light_registry import LEGACY_SIDES

SIDE_LIGHTS = {side: light for light, side in LEGACY_SIDES.items()}


def _lights(sides):
    """Return a mapping keyed by light id from one keyed by side, or refuse it."""
    if set(sides) - set(SIDE_LIGHTS):
        raise ValueError("Unknown side")
    return {SIDE_LIGHTS[side]: value for side, value in sides.items()}


def _sides(lights):
    """Return the legacy lights of a light-keyed mapping, keyed by side."""
    return {side: lights[light] for light, side in LEGACY_SIDES.items() if light in lights}


def owner_envelope(payload):
    """Light-keyed copy of a ``/power-owner`` envelope (states, capability revisions)."""
    if not isinstance(payload, dict):
        raise ValueError("Invalid power owner envelope")
    result = dict(payload)
    if type(payload.get("states")) is dict:
        result["states"] = _lights(payload["states"])
    capability = payload.get("power_requests")
    if type(capability) is dict and type(capability.get("revisions")) is dict:
        result["power_requests"] = {**capability, "revisions": _lights(capability["revisions"])}
    return result


def owner_observed(observed):
    """The observed native power of the legacy lights, keyed by side."""
    return _sides(observed)


def owner_exchange(result):
    """Side-keyed wire commands in a power request exchange result."""
    result = dict(result)
    if "power_requests" in result:
        result["power_requests"] = [
            {**{key: value for key, value in wire.items() if key != "light"},
             "role": LEGACY_SIDES[wire["light"]]}
            for wire in result["power_requests"]
        ]
    return result
