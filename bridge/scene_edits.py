"""Keep the light actions of a scene that the Hue app edits.

The pinned diyHue stores a scene's light actions only when the scene is created
(POST). An edit (PUT with ``actions``) keeps the name, palette and speed but
drops the actions, so a light kept its old stored state: a Govee lamp stored as
warm white stayed warm white whatever colour the owner chose in the app.

``edited_states`` checks and converts the whole action list as diyHue does on
create; ``replace`` then makes it the scene's light states. The bridge does both
before diyHue handles the request, so a refused edit changes nothing (not the
name, not a recall) and a recall in the same request uses the edited states. An
edit is the whole list: a light left out of it leaves the scene, and a stored
``ct`` does not stay next to a new colour. A room scene takes only lights of its
room, so an edit cannot bring a light that moved to another room back into this
room's scene.
"""

import weakref

# The gradient modes a Hue light accepts; any other is not stored.
GRADIENT_MODES = frozenset({"interpolated_palette", "interpolated_palette_mirrored",
                            "random_pixelated", "segmented_palette"})


class InvalidEdit(ValueError):
    """The edit has an action the bridge cannot store; nothing was changed."""


def _number(value, low, high):
    # The range check also refuses NaN and both infinities.
    return type(value) in (int, float) and low <= value <= high


def _xy(value):
    if not isinstance(value, dict) or not all(_number(value.get(axis), 0, 1) for axis in "xy"):
        raise InvalidEdit("Invalid xy colour")
    return value["x"], value["y"]


def _gradient(value):
    """A stored gradient built from the checked points only."""
    points = value.get("points") if isinstance(value, dict) else None
    if not isinstance(points, list) or not 2 <= len(points) <= 5:
        raise InvalidEdit("Invalid gradient")
    stored = []
    for point in points:
        color = point.get("color") if isinstance(point, dict) else None
        x, y = _xy(color.get("xy") if isinstance(color, dict) else None)
        stored.append({"color": {"xy": {"x": x, "y": y}}})
    gradient = {"points": stored}
    mode = value.get("mode")
    if isinstance(mode, str) and mode in GRADIENT_MODES:
        gradient["mode"] = mode
    return gradient


def light_state(action):
    """A diyHue light state from a v2 scene action, as diyHue converts it on create."""
    if not isinstance(action, dict):
        raise InvalidEdit("Invalid action")
    state = {}
    if "on" in action:
        if not isinstance(action["on"], dict) or type(action["on"].get("on")) is not bool:
            raise InvalidEdit("Invalid on")
        state["on"] = action["on"]["on"]
    if "dimming" in action:
        brightness = (action["dimming"].get("brightness")
                      if isinstance(action["dimming"], dict) else None)
        if not _number(brightness, 0, 100):
            raise InvalidEdit("Invalid brightness")
        state["bri"] = int(brightness * 2.54)
    if "color" in action:
        if not isinstance(action["color"], dict):
            raise InvalidEdit("Invalid colour")
        state["xy"] = list(_xy(action["color"].get("xy")))
    if "color_temperature" in action:
        mirek = (action["color_temperature"].get("mirek")
                 if isinstance(action["color_temperature"], dict) else None)
        if type(mirek) is not int or not 153 <= mirek <= 500:
            raise InvalidEdit("Invalid colour temperature")
        state["ct"] = mirek
    if "gradient" in action:
        state["gradient"] = _gradient(action["gradient"])
    return state


def members(scene):
    """The lights a scene may hold: its room's lights for a room scene, else None."""
    reference = getattr(scene, "group", None)
    group = reference() if callable(reference) else None
    if group is None:
        return None
    return {light for item in list(group.lights) if (light := item()) is not None}


def edited_states(scene, actions, lights):
    """``{light: state}`` for the edit, or InvalidEdit. Nothing is changed here.

    ``lights`` is diyHue's ``config["lights"]``. Every action must target a light;
    a room scene's actions must target lights of its room.
    """
    if not isinstance(actions, list):
        raise InvalidEdit("Invalid actions")
    by_v2 = {getattr(light, "id_v2", None): light for light in list(lights.values())}
    allowed = members(scene)
    states = {}
    for item in actions:
        target = item.get("target") if isinstance(item, dict) else None
        if not isinstance(target, dict) or target.get("rtype") != "light":
            raise InvalidEdit("Invalid target")
        rid = target.get("rid")
        light = by_v2.get(rid) if isinstance(rid, str) else None
        if light is None or (allowed is not None and light not in allowed):
            raise InvalidEdit("Target is not a light of this scene's room")
        states[light] = light_state(item.get("action"))
    return states


def replace(scene, states):
    """Make ``states`` the scene's light states; return whether anything changed.

    The new map is built whole and published in one assignment, so a concurrent
    reader (a recall, another edit) sees either the old states or the new ones.
    """
    if dict(scene.lightstates.items()) == states:
        return False
    scene.lightstates = weakref.WeakKeyDictionary(states)
    return True
