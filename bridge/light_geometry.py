"""Zone geometry of one layout row on the front wall.

A light's zones are the places where Sync samples the screen perimeter for it. Each
zone is a point (x, height) in layout centimeters; a segmented floor lamp also has
a vertical span per zone, because its visibility is averaged over the segment.
"""

from desk_layout import screen_size
from govee_models import MODELS

# Tested values per Govee model come from the capability table (govee_models).
GEOMETRIES = {model: entry["geometry"] for model, entry in MODELS.items()}
SCREEN = {"kind": "screen"}
POINT = {"kind": "point"}
_ROLE_GEOMETRY = {
    "govee-left": GEOMETRIES["H6076"],
    "govee-right": GEOMETRIES["H6076"],
    "hue-monitor": SCREEN,
    "play-left": POINT,
    "play-center": POINT,
    "play-right": POINT,
}
# A native light with several channels and no known geometry spreads them evenly
# along its orientation around its position, this far apart.
CHANNEL_SPACING_CM = 10
# The nine anchors of the monitor strip along the screen edge, as fractions of the
# screen from its center: up the left side, across the top, down the right side.
SCREEN_ANCHORS = (
    (-.5, -.5), (-.5, 0), (-.5, .5), (-.25, .5), (0, .5), (.25, .5), (.5, .5), (.5, 0), (.5, -.5),
)


def geometry_for(row):
    """Return the geometry entry of a layout row: its role, else its model, else a point."""
    identifier = row["id"]
    if identifier in _ROLE_GEOMETRY:
        return _ROLE_GEOMETRY[identifier]
    return GEOMETRIES.get(row.get("model"), POINT)


def segment_spans(row):
    """Return the (low, high) height of each segment, bottom to top; empty if unsegmented."""
    geometry = geometry_for(row)
    if geometry["kind"] != "segmented-floor":
        return []
    base = row["positionCm"]["height"] - geometry["anchor_offset_cm"]
    zones, first, span = geometry["zones"], geometry["first_cm"], geometry["span_cm"]
    return [(base + first + index * span / zones, base + first + (index + 1) * span / zones)
            for index in range(zones)]


def zone_points(row, screen, zones=1):
    """Return one (x, height) point per zone, bottom to top or along the screen edge.

    A point light with ``zones`` channels (a native Hue gradient light) spreads them
    left to right or bottom to top along its orientation, centered on its position.
    """
    kind = geometry_for(row)["kind"]
    if kind == "segmented-floor":
        x = row["positionCm"]["x"]
        return [(x, (low + high) / 2) for low, high in segment_spans(row)]
    if kind == "screen":
        # The screen object is authoritative for the screen's position and size.
        center = screen["positionCm"]
        width, height = screen_size({"screen": screen})
        return [(center["x"] + u * width, center["height"] + v * height)
                for u, v in SCREEN_ANCHORS]
    x, height = row["positionCm"]["x"], row["positionCm"]["height"]
    offsets = [(index - (zones - 1) / 2) * CHANNEL_SPACING_CM for index in range(zones)]
    if row.get("orientation") == "vertical":
        return [(x, height + offset) for offset in offsets]
    return [(x + offset, height) for offset in offsets]
