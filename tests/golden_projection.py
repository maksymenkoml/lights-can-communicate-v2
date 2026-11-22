"""Golden Sync and scene projection outputs for named desk layouts.

``python3 tests/golden_projection.py --write`` computes every output from the
projection modules and writes ``tests/golden/projection-v6.json``. The fixture
was generated from the code before the screen object and the generic per-light
projection existed (#7). It pins those outputs; it must not be regenerated to
make a later change pass.
"""

import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bridge"))
from desk_layout import positions, validate_layout  # noqa: E402
from sync_projection import (  # noqa: E402
    project_govee_frame,
    project_native_frame,
    scene_fade,
    sync_profile,
)
from sync_zone import zone_points  # noqa: E402

FIXTURE = ROOT / "tests" / "golden" / "projection-v6.json"
ROLE_ROWS = (
    # id, x (layout cm), height of the anchor, Sync scale, orientation
    ("govee-left", 20, 60, 65),
    ("govee-right", 180, 60, 65),
    ("hue-monitor", 100, 110, 100),
    ("play-left", 70, 40, 100),
    ("play-center", 100, 40, 50),
    ("play-right", 130, 40, 100),
)
REAL_ID = "device-" + "ab" * 16


def default_layout():
    """The layout Light Studio writes for a new installation (studio/static/app.js)."""
    return {
        "version": 6,
        "gridStepCm": 5,
        "room": "Office",
        "desk": {"width": 160, "height": 85},
        "monitorInches": 32,
        "syncZone": None,
        "sceneZone": None,
        "syncFalloff": True,
        "sceneFalloff": False,
        "devices": [
            {"id": key, "scale": scale, "sceneScale": scale, "orientation": "horizontal",
             "positionCm": {"x": x, "height": height}}
            for key, x, height, scale in ROLE_ROWS
        ],
    }


def row(layout, key):
    return next(item for item in layout["devices"] if item["id"] == key)


def zone(points):
    return {"version": 1, "points": [{"u": u, "h": h} for u, h in points]}


def legacy(version, layout):
    """Return ``layout`` as an older file: no scene fields, only the stored keys."""
    result = copy.deepcopy(layout)
    result["version"] = version
    for key in ("syncZone", "sceneZone", "syncFalloff", "sceneFalloff"):
        if version < 5 or result.get(key) is None:
            result.pop(key, None)
    if version < 5:
        for item in result["devices"]:
            item.pop("sceneScale", None)
    if version < 4:
        for item in result["devices"]:
            item.pop("syncRole", None)
    return result


def layouts():
    named = {}
    named["default"] = default_layout()

    layout = default_layout()
    row(layout, "hue-monitor")["positionCm"] = {"x": 60, "height": 135}
    layout["monitorInches"] = 43
    named["monitor-left-up-43in"] = layout

    layout = default_layout()
    layout["syncFalloff"] = False
    named["fade-off"] = layout

    layout = default_layout()
    layout["sceneFalloff"] = True
    layout["sceneZone"] = zone(
        [(-1.2, 0), (1.2, 0), (1.2, -40), (0.5, -45), (-0.5, -45), (-1.2, -40)])
    named["scene-fade-on-custom-zone"] = layout

    layout = default_layout()
    row(layout, "govee-right")["enabled"] = False
    named["govee-disabled"] = layout

    layout = default_layout()
    row(layout, "play-center")["syncRole"] = "own"
    named["play-sync-own"] = layout

    layout = default_layout()
    row(layout, "play-left")["enabled"] = False
    row(layout, "govee-left")["syncRole"] = "off"
    named["play-removed-govee-off"] = layout

    layout = default_layout()
    layout["syncZone"] = zone([(-1.2, 0), (1.2, 0), (1.2, -6), (0.5, -10), (-0.5, -10), (-1.2, -6)])
    named["custom-zone-hides-lower-segments"] = layout

    layout = default_layout()
    layout["syncFalloff"] = False
    layout["syncZone"] = zone([(-1.2, 0), (1.2, 0), (1.2, -6), (0.5, -10), (-0.5, -10), (-1.2, -6)])
    named["custom-zone-fade-off"] = layout

    # A raised zone fades the lower screen anchors and the Play lights.
    raised = zone([(-1.5, 40), (1.5, 40), (1.5, 20), (0.5, 15), (-0.5, 15), (-1.5, 20)])
    layout = default_layout()
    layout["syncZone"] = raised
    named["sync-zone-raised-fades-strip"] = layout

    layout = default_layout()
    layout["sceneFalloff"] = True
    layout["sceneZone"] = raised
    named["scene-zone-raised-fades-strip"] = layout

    layout = default_layout()
    row(layout, "govee-left")["positionCm"] = {"x": 55, "height": 120}
    row(layout, "govee-right")["positionCm"] = {"x": 150, "height": 90}
    named["govee-beside-screen-edge"] = layout

    layout = default_layout()
    layout["monitorInches"] = 55
    row(layout, "hue-monitor")["positionCm"] = {"x": 100, "height": 20}
    named["monitor-height-raised-by-minimum"] = layout

    layout = default_layout()
    layout["desk"] = {"width": 120, "height": 70}
    row(layout, "govee-left")["positionCm"] = {"x": 0, "height": 60}
    row(layout, "govee-right")["positionCm"] = {"x": 215, "height": 150}
    row(layout, "play-right")["positionCm"] = {"x": 190, "height": 25}
    row(layout, "play-left")["orientation"] = "vertical"
    named["small-desk-wide-spread"] = layout

    layout = default_layout()
    layout["devices"] += [
        {"id": "virtual-lamp", "scale": 80, "sceneScale": 80, "orientation": "horizontal",
         "shape": "bar", "name": "Spare", "positionCm": {"x": -150, "height": 40}},
        {"id": REAL_ID, "scale": 80, "sceneScale": 80, "orientation": "horizontal",
         "name": "Extra strip", "model": "Hue LCX006", "shape": "strip", "zones": 9,
         "positionCm": {"x": 330, "height": 50}},
    ]
    named["extra-rows-widen-the-default-zone"] = layout

    named["v5-file"] = legacy(5, named["monitor-left-up-43in"])
    named["v5-file"]["sceneFalloff"] = True
    named["v3-file"] = legacy(3, default_layout())
    named["v4-file"] = legacy(4, named["govee-disabled"])
    return named


def frames():
    gradient = {channel: (channel * 4800 % 65536, (65535 - channel * 3900) % 65536,
                          channel * channel * 311 % 65536)
                for channel in range(18)}
    primes = {channel: (channel * 7919 % 65536, channel * 104729 % 65536,
                        channel * 1299709 % 65536)
              for channel in range(18)}
    # A straight ramp over the twelve capture channels 2-13; the others stay black.
    ramp = {channel: ((channel - 2) * 5957, 65535 - (channel - 2) * 5957, (channel - 2) * 2978)
            if 2 <= channel <= 13 else (0, 0, 0)
            for channel in range(18)}
    return {"gradient": gradient, "primes": primes, "ramp-2-13": ramp}


def cases():
    return [{"name": name, "layout": layout,
             "frames": {key: {str(c): list(rgb) for c, rgb in value.items()}
                        for key, value in frames().items()}}
            for name, layout in layouts().items()]


def jsonable(value):
    return json.loads(json.dumps(value, allow_nan=False))


def compute(case):
    """Return every pinned output for a case: ``{"layout": raw, "frames": ..., ...}``."""
    layout = validate_layout(copy.deepcopy(case["layout"]))
    profile = sync_profile(layout)
    capture = {key: {int(c): tuple(rgb) for c, rgb in value.items()}
               for key, value in case["frames"].items()}
    result = {
        "native": {key: project_native_frame(frame, profile) for key, frame in capture.items()},
        "govee": {key: {side: project_govee_frame(frame, side, profile)
                        for side in ("left", "right")} for key, frame in capture.items()},
        "scene_fade": scene_fade(layout),
        "positions": {"gradient": positions(layout, True),
                      "plain": positions(layout, False)},
        "zone": {"syncZone": zone_points(layout), "sceneZone": zone_points(layout, "sceneZone")},
    }
    return jsonable(result)


def build():
    return {"cases": {case["name"]: {"layout": case["layout"], "frames": case["frames"],
                                     **compute(case)} for case in cases()}}


def main(argv):
    if argv[1:] != ["--write"]:
        print("usage: golden_projection.py --write")
        return 2
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(json.dumps(jsonable(build()), indent=1, sort_keys=True) + "\n")
    print("wrote", FIXTURE.relative_to(ROOT), "-", len(cases()), "cases", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
