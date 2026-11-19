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
import unicodedata
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
    "enabled",
    "syncRole",
    "sceneScale",
}
# Version 4 adds the optional row field ``syncRole``. Version 5 adds the second
# (scene) configuration: row ``sceneScale`` and top-level ``sceneZone``,
# ``syncFalloff`` and ``sceneFalloff``. Stored version 3 and 4 files load and are
# returned as version 5; writes must use LAYOUT_VERSION.
LAYOUT_VERSION = 5
LAYOUT_VERSIONS = (3, 4, LAYOUT_VERSION)
# Fade switches are stored only when they differ from these defaults.
FALLOFF_DEFAULTS = {"syncFalloff": True, "sceneFalloff": False}
# "off" switches a role lamp off for each Sync session (see sync_off.py); only the
# six role rows accept it. Discovered and unbound rows keep "sync" or "own".
SYNC_ROLES = ("sync", "own", "off")
# Owner name of a role lamp ("name" on the six role rows). The Hue roles are
# also written to the physical Hue light, whose name holds 32 characters.
NAME_LIMIT = 35
HUE_NAME_LIMIT = 32
HUE_LAYOUT_ROLES = ("hue-monitor", "play-left", "play-center", "play-right")


def owner_name(value, key):
    """Return the stripped owner name of role row ``key`` or raise ValueError."""
    limit = HUE_NAME_LIMIT if key in HUE_LAYOUT_ROLES else NAME_LIMIT
    if not isinstance(value, str):
        raise ValueError("Invalid lamp name")
    name = value.strip()
    if (not 1 <= len(name) <= limit
            or any(unicodedata.category(char).startswith("C") for char in name)):
        raise ValueError("Invalid lamp name")
    return name


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
    allowed = {"version", "gridStepCm", "room", "desk", "monitorInches", "devices", "syncZone",
               "sceneZone", *FALLOFF_DEFAULTS}
    version = value.get("version") if isinstance(value, dict) else None
    if (not isinstance(value, dict) or set(value) - allowed or type(version) is not int
            or version not in LAYOUT_VERSIONS):
        raise ValueError("Expected a version-three, version-four or version-five desk layout")
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
        "version": LAYOUT_VERSION,
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
        real = re.fullmatch(r"device-[a-f0-9]{32}", key) is not None
        if key not in ROLES and not re.fullmatch(r"virtual-[a-z0-9]{1,32}", key) and not real:
            raise ValueError("Unknown layout identity")
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
        enabled = row.get('enabled', True)
        if type(enabled) is not bool:
            raise ValueError('Invalid composition membership')
        if not enabled:
            device['enabled'] = False
        # "sync" is the default and is never stored; "own" keeps the lamp's own
        # light during Sync and "off" switches it off while Sync runs. Both exclude
        # the lamp from the composition; outside Sync it still gets scenes.
        sync_role = row.get("syncRole", "sync")
        if (type(sync_role) is not str or sync_role not in SYNC_ROLES
                or (sync_role == "off" and key not in ROLES)):
            raise ValueError("Invalid Sync role")
        if sync_role != "sync":
            device["syncRole"] = sync_role
        # The without-Sync correction is the row's own value. A row without it
        # (older files and clients) gets a copy of the Sync correction, which is
        # what scenes used before; it then changes independently.
        device["sceneScale"] = (number(row["sceneScale"], 0, 100)
                                if row.get("sceneScale") is not None else device["scale"])
        if key in ROLES and row.get("name") is not None:
            # Missing means "no owner name": nothing is renamed anywhere.
            device["name"] = owner_name(row["name"], key)
        if key.startswith("virtual-"):
            shape, name = row.get("shape"), row.get("name", "Нова лампа")
            if (
                shape not in ("floor", "strip", "bar")
                or not isinstance(name, str)
                or len(name) > 35
            ):
                raise ValueError("Invalid unbound drawing")
            device.update(shape=shape, name=name)
        if real:
            name, model, shape, zones = (row.get(field) for field in
                                        ("name", "model", "shape", "zones"))
            if (not isinstance(name, str) or not 1 <= len(name) <= 80
                    or not isinstance(model, str) or not 1 <= len(model) <= 64
                    or shape not in ("floor", "strip", "bar")
                    or type(zones) is not int or not 1 <= zones <= 32):
                raise ValueError("Invalid identity-bound layout metadata")
            device.update(name=name, model=model, shape=shape, zones=zones)
        device['positionCm']['height'] = max(height, minimum_anchor_height(
            device, result['monitorInches']))
        result["devices"].append(device)
    if not set(ROLES) <= seen:
        raise ValueError("The selected six device roles must remain present")
    if value.get('syncZone') is not None:
        from sync_zone import validate_zone

        result['syncZone'] = validate_zone(value['syncZone'])
    if value.get('sceneZone') is not None:
        from sync_zone import validate_zone

        result['sceneZone'] = validate_zone(value['sceneZone'])
    for field, default in FALLOFF_DEFAULTS.items():
        flag = value.get(field, default)
        if type(flag) is not bool:
            raise ValueError("Invalid fade switch")
        if flag is not default:
            result[field] = flag
    return result


def composed(device):
    """A validated row takes part in the Sync composition ("own" and "off" do not)."""
    return device.get("enabled", True) and device.get("syncRole", "sync") == "sync"


def minimum_anchor_height(device, monitor_inches):
    """Keep the complete schematic body above the floor on the five-cm grid."""
    identifier = device['id']
    if identifier == 'hue-monitor':
        display_height = monitor_inches * 2.54 * 9 / math.hypot(16, 9)
        return math.ceil((display_height / 2 + 4.8) / 5) * 5
    shape = ('floor' if identifier in ('govee-left','govee-right') else
             'bar' if identifier in ('play-left','play-center','play-right')
             else device.get('shape'))
    if shape == 'floor':
        return 60
    return 15 if shape == 'bar' and device.get('orientation') == 'vertical' else 5


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
        self.fade_cache = None

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

    def names(self):
        """Return ``{role: name}`` for the role lamps that have an owner name."""
        data = self.snapshot()
        if not data:
            return {}
        return {ROLES[device["id"]]: device["name"] for device in data["devices"]
                if device["id"] in ROLES and "name" in device}

    def scene_scale(self, role, fallback):
        """Return the without-Sync correction (row ``sceneScale``)."""
        data = self.snapshot()
        if data:
            for device in data["devices"]:
                if ROLES.get(device["id"]) == role:
                    return device["sceneScale"] / 100
        return fallback

    def _member(self, role, fallback, test):
        data = self.snapshot()
        if data:
            for device in data['devices']:
                if device['id'] == role or ROLES.get(device['id']) == role:
                    return test(device)
        elif self.path.exists():
            # A corrupt saved selection is not permission to resume every lamp.
            return False
        return fallback

    def enabled(self, role, fallback=True):
        """Return Sync composition membership: removed and own-light rows are excluded."""
        return self._member(role, fallback, composed)

    def present(self, role, fallback=True):
        """Return scene membership: every lamp that is not removed, own light included."""
        return self._member(role, fallback, lambda device: device.get("enabled", True))

    def sync_off_roles(self):
        """Return the present role lamps that Sync switches off ("off"), in layout order."""
        data = self.snapshot()
        if not data:
            return []
        return [ROLES[device["id"]] for device in data["devices"]
                if device["id"] in ROLES and device.get("enabled", True)
                and device.get("syncRole") == "off"]

    def scene_fade(self):
        """Return the scene fade gains per role (all 1 while the fade is off), or None."""
        from sync_projection import scene_fade

        with self.lock:
            data = self.snapshot()
            if data is None:
                return None
            if self.fade_cache is None or self.fade_cache[0] != self.signature:
                self.fade_cache = (self.signature, scene_fade(data))
            return copy.deepcopy(self.fade_cache[1])

    def sync_profile(self):
        from sync_projection import sync_profile

        data = self.snapshot()
        return sync_profile(data) if data is not None else None

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
