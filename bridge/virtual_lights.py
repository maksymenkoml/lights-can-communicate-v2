"""Virtual bridge lights of the enrolled Govee lights, from the light registry.

The gateway creates one virtual light per enrolled light. The two migrated Govee
lamps keep slots 1 and 2 and their seeds; a light enrolled later has the slot
and seed the registry gave it. The four native Hue lights keep slots 3-6 and are
handled by role elsewhere. A removed light keeps its virtual light, unreachable
and not driven, so a rejoin finds the same light and its scenes.
"""

import sys
from pathlib import Path

from govee_models import target_capabilities

# The core archive, mounted read-only next to the gateway modules.
ARCHIVE = Path("/probe/lights-can-communicate-v2.pyz")
REGISTRY_FILE = "lights.json"
# Without a registry the gateway keeps the migrated lamps.
LEGACY = (
    {"slot": "1", "light": "govee-left", "seed": "Left", "model": "H6076", "segmented": True},
    {"slot": "2", "light": "govee-right", "seed": "Right", "model": "H6076", "segmented": True},
)
LEGACY_SLOTS = frozenset(item["slot"] for item in LEGACY)


def _registry_class():
    # The release mounts the archive; a source checkout has the package next door.
    source = Path(__file__).resolve().parents[1]
    for path, present in ((ARCHIVE, ARCHIVE.is_file()),
                          (source, (source / "lights_can_communicate_v2_core").is_dir())):
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


def signature(state):
    """A comparable summary of the virtual lights the registry asks for."""
    lights, retired = plan(load(state))
    return (tuple((item["slot"], item["light"], item["segmented"]) for item in lights),
            tuple(sorted(retired)))


def default_name(item):
    """The name of a new virtual light before the owner names it in Light Studio."""
    if item["light"] == "govee-left":
        return "Govee Left"
    if item["light"] == "govee-right":
        return "Govee Right"
    return f"Govee {item['model']} {item['light'][-4:]}"
