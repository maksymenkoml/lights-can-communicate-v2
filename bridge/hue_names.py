"""Display names of the virtual Hue bridge and its resources.

Names are display-only: the Hue app and the Sync Box refer to resources by ID,
so renaming never affects pairing. Early builds created resources with test
markers; those exact names are replaced on start, while any name the owner
chose in the Hue app is left alone.
"""

BRIDGE_NAME = "Lights Can Communicate v2"
ROOM_NAME = "Office"
AREA_NAME = "Desk Sync"
MONITOR_NAME = "Hue Monitor"


def govee_name(side):
    return f"Govee {side}"


def hue_play_name(side):
    return f"Hue Play {side}"


# The diyHue default for a new bridge and the names earlier builds forced.
LEGACY_BRIDGE_NAMES = frozenset({
    "DiyHue Bridge", "Smart Lighting Probe", "Lights Can Communicate v2 Probe",
})

LEGACY_NAMES = {
    "lights": {
        "Govee Left TEST": govee_name("Left"),
        "Govee Right TEST": govee_name("Right"),
        "Hue Monitor PROXY": MONITOR_NAME,
        **{f"Hue Play {side} PROXY": hue_play_name(side)
           for side in ("Left", "Center", "Right")},
    },
    "groups": {"Probe Office TEST": ROOM_NAME, "Desk Sync PILOT": AREA_NAME},
    "scenes": {"Probe Violet Amber TEST": "Violet Amber"},
}


def rename_legacy(config):
    """Replace default and exact legacy test names in place."""
    settings = config.get("config", {})
    if settings.get("name") in LEGACY_BRIDGE_NAMES:
        settings["name"] = BRIDGE_NAME
    for section, names in LEGACY_NAMES.items():
        for resource in config.get(section, {}).values():
            replacement = names.get(getattr(resource, "name", None))
            if replacement is not None:
                resource.name = replacement


# Virtual light slot of each adapter role (see ``BridgeOutput.slots``).
SLOT_ROLES = {"1": "govee-left", "2": "govee-right", "3": "strip",
              "4": "hue_left", "5": "hue_center", "6": "hue_right"}


class OwnerNames:
    """Give the virtual lights the names the owner set in Light Studio.

    ``names()`` returns ``{role: name}`` for the roles that have an owner name.
    A light is renamed when its owner name differs from the last one this
    object applied, which is at the first call and after each layout change.
    Without an owner name the light keeps its current name, and a later rename
    in the Hue app stays until the owner changes the name in Light Studio.
    ``lights`` maps slot ids to diyHue lights; a diyHue light keeps one name
    that serves the v1 ``name`` and the v2 ``metadata.name``. Resource IDs
    never change. ``save`` persists the bridge configuration and ``publish``
    announces the renamed lights to the Hue app (skipped with ``announce=False``
    at start); both run once per apply.
    """

    def __init__(self, lights, slots, names, save=lambda: None, publish=lambda lights: None):
        self.lights, self.slots, self.names = lights, slots, names
        self.save, self.publish = save, publish
        self.applied = {}

    def apply(self, announce=True):
        """Rename the lights whose owner name changed; return their slot ids."""
        owner = self.names()
        renamed = []
        for slot, role in self.slots.items():
            name = owner.get(role)
            lamp = self.lights.get(slot)
            if name is None or lamp is None or self.applied.get(slot) == name:
                continue
            self.applied[slot] = name
            if lamp.name != name:
                lamp.name = name
                renamed.append(slot)
        if renamed:
            self.save()
            if announce:
                self.publish([self.lights[slot] for slot in renamed])
        return renamed
