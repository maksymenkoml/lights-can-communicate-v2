"""Display names of the virtual Hue bridge and its resources.

Names are display-only: the Hue app and the Sync Box refer to resources by ID,
so renaming never affects pairing. Early pilots created resources with test
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


# The diyHue default for a new bridge and the names earlier pilots forced.
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
    """Replace default and exact pilot test names in place."""
    settings = config.get("config", {})
    if settings.get("name") in LEGACY_BRIDGE_NAMES:
        settings["name"] = BRIDGE_NAME
    for section, names in LEGACY_NAMES.items():
        for resource in config.get(section, {}).values():
            replacement = names.get(getattr(resource, "name", None))
            if replacement is not None:
                resource.name = replacement
