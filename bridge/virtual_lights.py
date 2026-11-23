"""Virtual bridge lights of the enrolled lights, from the light registry.

The gateway creates one virtual light per enrolled light. The two migrated Govee
lamps keep slots 1 and 2 and their seeds; a light enrolled later has the slot
and seed the registry gave it. The four migrated native Hue lights keep slots 3-6
and are handled by role elsewhere; a Hue light enrolled later is planned here
(see hue_plan). A removed light keeps its virtual light, unreachable and not
driven, so a rejoin finds the same light and its scenes.
"""

import sys
from pathlib import Path

from govee_models import target_capabilities

# The core archive, which the release mounts read-only next to the gateway modules.
ARCHIVE = "lights-can-communicate-v2.pyz"
PACKAGE = "lights_can_communicate_v2_core"
REGISTRY_FILE = "lights.json"
# Without a registry the gateway keeps the migrated lamps.
LEGACY = (
    {"slot": "1", "light": "govee-left", "seed": "Left", "model": "H6076", "segmented": True},
    {"slot": "2", "light": "govee-right", "seed": "Right", "model": "H6076", "segmented": True},
)
LEGACY_SLOTS = frozenset(item["slot"] for item in LEGACY)
# The migrated native Hue lights; the gateway gives them slots 3-6 by role.
MIGRATED_HUE = ("hue-monitor", "play-left", "play-center", "play-right")


def _registry_class():
    # The release mounts the archive next to this module. The development
    # Compose file mounts the package there, and a source checkout has it one
    # level up.
    here = Path(__file__).resolve().parent
    for path, present in ((here / ARCHIVE, (here / ARCHIVE).is_file()),
                          (here, (here / PACKAGE).is_dir()),
                          (here.parent, (here.parent / PACKAGE).is_dir())):
        if present and str(path) not in sys.path:
            sys.path.append(str(path))
    from lights_can_communicate_v2_core.light_registry import LightRegistry

    return LightRegistry


def load(state):
    """Return the validated registry data of the state directory, or None."""
    path = Path(state) / REGISTRY_FILE
    if not path.exists():
        return None
    try:
        return _registry_class().load(path).to_dict()
    except (ImportError, ValueError):
        return None


def plan(data):
    """Return (Govee lights, retired slots) for registry data or None (legacy).

    Each Govee light is ``{slot, light, seed, model, segmented}`` in slot order.
    A migrated lamp without a model keeps the values it always had.
    """
    if data is None:
        return [dict(item) for item in LEGACY], frozenset()
    lights = []
    for item in data["lights"]:
        if item["provider"] != "govee":
            continue
        govee = item["govee"]
        model = govee.get("sku") or govee.get("model")
        caps = target_capabilities({"light": item["id"], **govee})
        lights.append({
            "slot": item["virtual"]["id_v1"], "light": item["id"],
            "seed": item["virtual"]["seed"], "model": model or "H6076",
            "segmented": caps["tier"] == "segmented",
        })
    lights.sort(key=lambda item: int(item["slot"]))
    retired = frozenset(record["virtual"]["id_v1"] for record in data.get("retired", []))
    return lights, retired


def hue_plan(data):
    """Return the Hue lights enrolled after the migration, in slot order.

    Each is ``{slot, light, seed, model, channels}``. A light with several channels
    is a gradient light, any other one a color bulb, like its native light.
    """
    if data is None:
        return []
    lights = [{"slot": item["virtual"]["id_v1"], "light": item["id"],
               "seed": item["virtual"]["seed"], "model": item["hue"].get("model", "unknown"),
               "channels": len(item["hue"]["channels"])}
              for item in data["lights"]
              if item["provider"] == "hue" and item["id"] not in MIGRATED_HUE]
    return sorted(lights, key=lambda item: int(item["slot"]))


def signature(state):
    """A comparable summary of the virtual lights the registry asks for."""
    data = load(state)
    lights, retired = plan(data)
    return (tuple((item["slot"], item["light"], item["segmented"]) for item in lights),
            tuple(sorted(retired)),
            tuple((item["slot"], item["light"], item["channels"]) for item in hue_plan(data)))


def default_name(item):
    """The name of a new virtual light before the owner names it in Light Studio."""
    if "channels" in item:
        return f"Hue {item['model']} {item['light'][-4:]}"
    if item["light"] == "govee-left":
        return "Govee Left"
    if item["light"] == "govee-right":
        return "Govee Right"
    return f"Govee {item['model']} {item['light'][-4:]}"
