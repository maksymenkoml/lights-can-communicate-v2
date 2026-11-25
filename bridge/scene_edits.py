"""Keep the light actions of a scene that the Hue app edits.

The pinned diyHue stores a scene's light actions only when the scene is created
(POST). An edit (PUT with ``actions``) keeps the name, palette and speed but
drops the actions, so a light kept its old stored state: a Govee lamp stored as
warm white stayed warm white whatever colour the owner chose in the app.

``apply_actions`` converts the actions as diyHue does on create and replaces each
targeted light's stored state. Replacing matters: a merged state would keep an old
``ct`` next to a new colour, and the light would still show the old white.
"""


def light_state(action):
    """A diyHue light state from a v2 scene action, as diyHue converts it on create."""
    state = {}
    if isinstance(action.get("on"), dict) and type(action["on"].get("on")) is bool:
        state["on"] = action["on"]["on"]
    dimming = action.get("dimming")
    if isinstance(dimming, dict) and type(dimming.get("brightness")) in (int, float):
        state["bri"] = int(dimming["brightness"] * 2.54)
    color = action.get("color")
    if isinstance(color, dict) and isinstance(color.get("xy"), dict):
        state["xy"] = [color["xy"]["x"], color["xy"]["y"]]
    temperature = action.get("color_temperature")
    if isinstance(temperature, dict) and type(temperature.get("mirek")) is int:
        state["ct"] = temperature["mirek"]
    if isinstance(action.get("gradient"), dict):
        state["gradient"] = action["gradient"]
    return state


def apply_actions(scene, actions, lights):
    """Store the edited actions in ``scene``; return how many lights changed.

    ``lights`` is diyHue's ``config["lights"]``. An action for an unknown light,
    or not for a light, is skipped, as diyHue does on create.
    """
    by_v2 = {getattr(light, "id_v2", None): light for light in list(lights.values())}
    changed = 0
    for item in actions if isinstance(actions, list) else []:
        target = item.get("target") if isinstance(item, dict) else None
        if not isinstance(target, dict) or target.get("rtype") != "light":
            continue
        light = by_v2.get(target.get("rid"))
        action = item.get("action")
        if light is None or not isinstance(action, dict):
            continue
        state = light_state(action)
        if state and scene.lightstates.get(light) != state:
            scene.lightstates[light] = state
            changed += 1
    return changed
