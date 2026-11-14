"""Validated front-wall geometry for the six selected desk light roles."""

import copy
import hashlib
import json
import math
import os
import re
import tempfile
import threading
import time
import uuid
from pathlib import Path

ROLES = {
    "govee-left": "left",
    "govee-right": "right",
    "hue-monitor": "strip",
    "play-left": "hue_left",
    "play-center": "hue_center",
    "play-right": "hue_right",
}
PREVIEW_FIELDS = {
    "id",
    "scale",
    "positionCm",
    "orientation",
    "shape",
    "model",
    "name",
    "short",
    "zones",
    "rate",
}


def layout_event(resource):
    """Match the base bridge's complete Entertainment update event contract."""
    return {
        "creationtime": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "id": str(uuid.uuid4()),
        "type": "update",
        "data": [copy.deepcopy(resource)],
    }


def number(value, low, high):
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError("Invalid layout number")
    return value


def validate_layout(value):
    allowed = {"version", "gridStepCm", "room", "desk", "monitorInches", "devices"}
    if not isinstance(value, dict) or set(value) - allowed or value.get("version") != 3:
        raise ValueError("Expected a version-three desk layout")
    if value.get("gridStepCm", 5) != 5:
        raise ValueError("Desk placement requires a five-centimeter grid")
    desk = value.get("desk")
    if not isinstance(desk, dict) or set(desk) != {"width", "height"}:
        raise ValueError("Expected desk dimensions")
    room = value.get("room", "Mykola Office")
    if not isinstance(room, str) or not 1 <= len(room) <= 40:
        raise ValueError("Invalid room name")
    devices = value.get("devices")
    if not isinstance(devices, list) or not 6 <= len(devices) <= 12:
        raise ValueError("Expected the six selected lamps and optional unbound drawings")
    result = {
        "version": 3,
        "gridStepCm": 5,
        "room": room,
        "desk": {
            "width": number(desk["width"], 80, 240),
            "height": number(desk["height"], 50, 120),
        },
        "monitorInches": number(value.get("monitorInches"), 19, 55),
        "devices": [],
    }
    seen = set()
    for row in devices:
        if not isinstance(row, dict) or set(row) - PREVIEW_FIELDS:
            raise ValueError("Unknown layout device fields")
        key = row.get("id")
        if not isinstance(key, str) or key in seen:
            raise ValueError("Invalid or duplicate layout device")
        if key not in ROLES and not re.fullmatch(r"virtual-[a-z0-9]{1,32}", key):
            raise ValueError("Only selected roles and unbound drawings are allowed")
        seen.add(key)
        point = row.get("positionCm")
        if not isinstance(point, dict) or set(point) != {"x", "height"}:
            raise ValueError("Expected centimeter coordinates")
        x, height = number(point["x"], -200, 400), number(point["height"], 0, 300)
        if any(not math.isclose(v / 5, round(v / 5), abs_tol=1e-7) for v in (x, height)):
            raise ValueError("Position must be on the five-centimeter grid")
        orientation = row.get("orientation", "horizontal")
        if orientation not in ("horizontal", "vertical"):
            raise ValueError("Unknown orientation")
        device = {
            "id": key,
            "scale": number(row.get("scale"), 0, 100),
            "positionCm": {"x": x, "height": height},
            "orientation": orientation,
        }
        if key.startswith("virtual-"):
            shape, name = row.get("shape"), row.get("name", "Нова лампа")
            if (
                shape not in ("floor", "strip", "bar")
                or not isinstance(name, str)
                or len(name) > 35
            ):
                raise ValueError("Invalid unbound drawing")
            device.update(shape=shape, name=name)
        result["devices"].append(device)
    if not set(ROLES) <= seen:
        raise ValueError("The selected six device roles must remain present")
    return result


def positions(layout, gradient=True):
    devices = {row["id"]: row for row in layout["devices"] if row["id"] in ROLES}
    monitor = devices["hue-monitor"]["positionCm"]
    width = layout["monitorInches"] * 2.54 * 16 / math.hypot(16, 9)
    height = width * 9 / 16

    def point(x, h):
        return (
            round(max(-1, min(1, (x - monitor["x"]) / width * 0.8)), 6),
            0.8,
            round(max(-1, min(1, (h - monitor["height"]) / height * 0.8)), 6),
        )

    result = [(0, 0.8, 0)] * (18 if gradient else 14)
    for key, channels in (("govee-left", [0, 14, 15]), ("govee-right", [1, 16, 17])):
        position = devices[key]["positionCm"]
        # The schematic's floor-lamp anchor is 60 cm above its base.
        base = position["height"] - 60
        fractions = [0.15, 0.5, 0.85] if gradient else [0.5]
        for channel, fraction in zip(channels, fractions):
            result[channel] = point(position["x"], base + 136 * fraction)
        if gradient:
            low, high = result[channels[0]][2], result[channels[2]][2]
            if high - low < 0.1:
                low = max(-1, min(0.9, (low + high) / 2 - 0.05))
                high = low + 0.1
            for channel, z in zip(channels, (low, (low + high) / 2, high)):
                result[channel] = (result[channel][0], 0.8, round(z, 6))
    result[2:11] = [
        (-0.4, 0.8, -0.4),
        (-0.4, 0.8, 0),
        (-0.4, 0.8, 0.4),
        (-0.22, 0.8, 0.4),
        (0, 0.8, 0.4),
        (0.22, 0.8, 0.4),
        (0.4, 0.8, 0.4),
        (0.4, 0.8, 0),
        (0.4, 0.8, -0.4),
    ]
    for key, channel in (("play-left", 11), ("play-center", 12), ("play-right", 13)):
        position = devices[key]["positionCm"]
        result[channel] = point(position["x"], position["height"])
    return result


class LayoutStore:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.signature = None
        self.data = None

    def snapshot(self):
        with self.lock:
            try:
                stat = self.path.stat()
                signature = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
                if signature != self.signature:
                    data = validate_layout(json.loads(self.path.read_text()))
                    self.data, self.signature = data, signature
            except (OSError, ValueError, TypeError):
                pass
            return copy.deepcopy(self.data)

    @staticmethod
    def digest(data):
        return (
            hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest() if data else None
        )

    def revision(self):
        return self.digest(self.snapshot())

    def response(self):
        data = self.snapshot()
        return {"layout": data, "revision": self.digest(data)}

    def scale(self, role, fallback):
        data = self.snapshot()
        if data:
            for device in data["devices"]:
                if ROLES.get(device["id"]) == role:
                    return device["scale"] / 100
        return fallback

    def save(self, value):
        data = validate_layout(value)
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w", prefix="tmp_desk_layout_", dir=self.path.parent, delete=False
                ) as stream:
                    temporary = Path(stream.name)
                    json.dump(data, stream, allow_nan=False)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            self.signature = None
            self.snapshot()
            return {"layout": copy.deepcopy(data), "revision": self.digest(data)}
