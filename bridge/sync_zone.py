"""Pure table-relative Sync zone validation and vertical polygon sections."""

import math

_ERROR = "Invalid Sync zone"


def _number(value, limit):
    # Check bounds first so even arbitrarily large JSON integers fail safely.
    if (type(value) not in (int, float) or not -limit <= value <= limit
            or not math.isfinite(value)):
        raise ValueError(_ERROR)
    return value


def _cross(a, b, c):
    return ((b["x"] - a["x"]) * (c["height"] - a["height"])
            - (b["height"] - a["height"]) * (c["x"] - a["x"]))


def _on_segment(a, b, p):
    return (min(a["x"], b["x"]) <= p["x"] <= max(a["x"], b["x"])
            and min(a["height"], b["height"]) <= p["height"]
            <= max(a["height"], b["height"]))


def _intersects(a, b, c, d):
    first, second = _cross(a, b, c), _cross(a, b, d)
    third, fourth = _cross(c, d, a), _cross(c, d, b)
    if ((first < 0 < second or second < 0 < first)
            and (third < 0 < fourth or fourth < 0 < third)):
        return True
    return (first == 0 and _on_segment(a, b, c)
            or second == 0 and _on_segment(a, b, d)
            or third == 0 and _on_segment(c, d, a)
            or fourth == 0 and _on_segment(c, d, b))


def _intersections(points, x_cm):
    heights = []
    for first, second in zip(points, points[1:] + points[:1]):
        x0, x1 = first["x"], second["x"]
        if not min(x0, x1) <= x_cm <= max(x0, x1):
            continue
        if x0 == x1:
            heights.extend((first["height"], second["height"]))
        elif x_cm == x0:
            heights.append(first["height"])
        elif x_cm == x1:
            heights.append(second["height"])
        else:
            fraction = (x_cm - x0) / (x1 - x0)
            heights.append(first["height"] + fraction * (second["height"] - first["height"]))
    # Endpoints use their original height above, so shared vertices deduplicate
    # exactly without merging distinct, very close parts of a narrow zone.
    return sorted(set(heights))


def validate_zone(value):
    """Return an independent canonical zone, or None for automatic geometry."""
    if value is None:
        return None
    if (not isinstance(value, dict) or set(value) != {"version", "points"}
            or type(value["version"]) is not int or value["version"] != 1
            or not isinstance(value["points"], list) or len(value["points"]) != 6):
        raise ValueError(_ERROR)
    result = {"version": 1, "points": []}
    for point in value["points"]:
        if not isinstance(point, dict) or set(point) != {"u", "h"}:
            raise ValueError(_ERROR)
        result["points"].append({"u": _number(point["u"], 20),
                                 "h": _number(point["h"], 400)})
    points = [{"x": point["u"], "height": point["h"]}
              for point in result["points"]]
    if len({(p["x"], p["height"]) for p in points}) != 6:
        raise ValueError(_ERROR)
    if not (points[0]["x"] < points[1]["x"]
            and points[5]["x"] < points[4]["x"] < points[3]["x"] < points[2]["x"]):
        raise ValueError(_ERROR)
    left, right = points[:2]
    slope = (right["height"] - left["height"]) / (right["x"] - left["x"])
    for point in points[2:]:
        top = left["height"] + slope * (point["x"] - left["x"])
        if top - point["height"] < 1:
            raise ValueError(_ERROR)
    edges = list(zip(points, points[1:] + points[:1]))
    for index, (first, second) in enumerate(edges):
        for other in range(index + 1, len(edges)):
            if other == index + 1 or index == 0 and other == len(edges) - 1:
                continue
            if _intersects(first, second, *edges[other]):
                raise ValueError(_ERROR)
    area = sum(a["x"] * b["height"] - b["x"] * a["height"] for a, b in edges)
    if area >= 0:
        raise ValueError(_ERROR)
    xs = sorted({point["x"] for point in points})
    sections = xs + [(first + second) / 2 for first, second in zip(xs, xs[1:])]
    if any(len(_intersections(points, x)) > 2 for x in sections):
        raise ValueError(_ERROR)
    return result


# A zone without a piece under the screen is drawn against the floor at the room axis.
FLOOR_REFERENCE = {"x": 100, "width": 200, "height": 0}


def _reference(layout):
    """The piece a zone is relative to: the screen's support, else the floor."""
    from desk_layout import screen_support

    return screen_support(layout) or FLOOR_REFERENCE


def default_zone(layout):
    """Build the automatic world polygon with the existing 15 cm side fade below the
    top of the screen's support, or None when the screen stands on nothing."""
    from desk_layout import screen_support

    support = screen_support(layout)
    if support is None:
        return None
    desk_height = support["height"]
    half_width = support["width"] / 2
    desk_left, desk_right = support["x"] - half_width, support["x"] + half_width
    monitor_x = layout["screen"]["positionCm"]["x"]
    monitor_half_width = layout["screen"]["inches"] * 2.54 * 8 / math.hypot(16, 9)
    device_xs = [row["positionCm"]["x"] for row in layout["devices"]
                 if row.get("enabled", True)]
    left = min([desk_left - 50, monitor_x - monitor_half_width - 20]
               + [x - 20 for x in device_xs])
    right = max([desk_right + 50, monitor_x + monitor_half_width + 20]
                + [x + 20 for x in device_xs])
    left, right = math.floor(left / 5) * 5, math.ceil(right / 5) * 5
    return [
        {"x": left, "height": desk_height},
        {"x": right, "height": desk_height},
        {"x": right, "height": desk_height - 15 - max(0, right - desk_right)},
        {"x": desk_right, "height": desk_height - 15},
        {"x": desk_left, "height": desk_height - 15},
        {"x": left, "height": desk_height - 15 - max(0, desk_left - left)},
    ]


def zone_points(layout, key="syncZone"):
    """Resolve a saved zone (``syncZone`` or ``sceneZone``) into world points, or None
    without a saved zone and a piece under the screen. A saved zone is relative to
    the screen's support (``u`` a fraction of its half width, ``h`` from its top)."""
    zone = validate_zone(layout.get(key))
    if zone is None:
        return default_zone(layout)
    reference = _reference(layout)
    return [{"x": reference["x"] + point["u"] * reference["width"] / 2,
             "height": reference["height"] + point["h"]}
            for point in zone["points"]]


def vertical_bounds(points, x_cm):
    """Return lower/upper world heights, including caps and pinched endpoints."""
    heights = _intersections(points, x_cm)
    if not heights:
        return None
    return heights[0], heights[-1]
