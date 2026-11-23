"""Core-owned registry of the enrolled lights; the adapter reads only this file."""

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from .native_profile import MAX_CHANNELS, NativeHueProfile, NativeProfileError

REGISTRY_FILE = "lights.json"
LEGACY_SIDES = {"govee-left": "left", "govee-right": "right"}
_MAX_REGISTRY_BYTES = 64 * 1024
# A Govee light is one of the two migrated lamps or a discovered device.
GOVEE_ID = re.compile(r"(?:govee-left|govee-right|device-[0-9a-f]{32})\Z")
# Only a discovered device can be enrolled or removed by the owner.
DEVICE_ID = re.compile(r"device-[0-9a-f]{32}\Z")
# Removed lights keep their virtual identity, so a rejoin gets the same virtual
# light and HomeKit accessory; the oldest records go first beyond this bound.
_MAX_RETIRED = 64
# The only tier an owner can force; every model in the capability table is segmented.
_GOVEE_TIERS = ("basic",)
_ID_V1 = re.compile(r"[1-9][0-9]{0,3}\Z")
_SEED = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
# The four migrated Hue lights; they come and go together. A Hue light enrolled
# later is a discovered device of the paired Entertainment area.
_HUE_LIGHTS = ("hue-monitor", "play-left", "play-center", "play-right")
HUE_ID = re.compile(r"(?:hue-monitor|play-left|play-center|play-right|device-[0-9a-f]{32})\Z")
MODEL_ID = re.compile(r"[A-Za-z0-9_.-]{1,64}\Z")


# Virtual bridge slot and identity seed of each migrated light (see bridge/bootstrap.py).
_SEEDS = {
    "govee-left": ("1", "Left"),
    "govee-right": ("2", "Right"),
    "hue-monitor": ("3", "HueMonitorStrip"),
    "play-left": ("4", "HuePlayLeft"),
    "play-center": ("5", "HuePlayCenter"),
    "play-right": ("6", "HuePlayRight"),
}


class RegistryError(ValueError):
    """A registry is invalid or unreadable; messages never include input values."""


def _invalid():
    return RegistryError("Light registry fields are invalid.")


def _object(value, fields):
    if type(value) is not dict or value.keys() != set(fields):
        raise _invalid()
    return value


def _text(value, pattern):
    if type(value) is not str or pattern.match(value) is None:
        raise _invalid()
    return value


def _uuid(value):
    if type(value) is not str:
        raise _invalid()
    try:
        parsed = UUID(value)
    except ValueError:
        raise _invalid() from None
    if parsed.int == 0 or str(parsed) != value:
        raise _invalid()
    return value


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RegistryError("Light registry contains duplicate fields.")
        result[key] = value
    return result


def _govee(value):
    if type(value) is not dict or "side" in value:
        raise _invalid()
    for key in value:
        if type(key) is not str:
            raise _invalid()
    for key in ("ip", "device"):
        if type(value.get(key)) is not str or not value[key]:
            raise _invalid()
    if "tier" in value and value["tier"] not in _GOVEE_TIERS:
        raise _invalid()
    return value


def _hue(value):
    value = _object(value, ("light", "channels", *(("model",) if type(value) is dict
                                                     and "model" in value else ())))
    _uuid(value["light"])
    if "model" in value:
        _text(value["model"], MODEL_ID)
    channels = value["channels"]
    if type(channels) is not list:
        raise _invalid()
    for channel in channels:
        channel = _object(channel, ("channel_id", "rid", "index"))
        _uuid(channel["rid"])
        for key in ("channel_id", "index"):
            if type(channel[key]) is not int:
                raise _invalid()
    return value


def _light(value):
    if type(value) is not dict or type(value.get("provider")) is not str:
        raise _invalid()
    provider = value["provider"]
    if provider not in ("govee", "hue"):
        raise _invalid()
    value = _object(value, ("id", "provider", "virtual", provider))
    _text(value["id"], GOVEE_ID if provider == "govee" else HUE_ID)
    virtual = _object(value["virtual"], ("id_v1", "seed"))
    _text(virtual["id_v1"], _ID_V1)
    _text(virtual["seed"], _SEED)
    (_govee if provider == "govee" else _hue)(value[provider])
    return value


def _validated(data):
    """Return a normalized private copy of a valid registry or raise RegistryError."""
    if type(data) is not dict:
        raise _invalid()
    lights = data.get("lights")
    if type(lights) is not list or not lights:
        raise _invalid()
    has_hue = any(type(item) is dict and item.get("provider") == "hue" for item in lights)
    optional = (("hue",) if has_hue else ()) + (("retired",) if "retired" in data else ())
    root = _object(data, ("version", "lights", *optional))
    if type(root["version"]) is not int or root["version"] != 1:
        raise RegistryError("Light registry version is unsupported.")
    if has_hue:
        _uuid(_object(root["hue"], ("area_id",))["area_id"])
    for item in lights:
        _light(item)
    retired = root.get("retired", [])
    if "retired" in root and (type(retired) is not list or not retired
                              or len(retired) > _MAX_RETIRED):
        raise _invalid()
    for record in retired:
        record = _object(record, ("id", "virtual"))
        _text(record["id"], DEVICE_ID)
        virtual = _object(record["virtual"], ("id_v1", "seed"))
        _text(virtual["id_v1"], _ID_V1)
        _text(virtual["seed"], _SEED)
    # Active and retired lights share one space of ids, slots and seeds.
    records = lights + retired
    ids = [item["id"] for item in records]
    if (len(set(ids)) != len(ids)
            or len({item["virtual"]["id_v1"] for item in records}) != len(ids)
            or len({item["virtual"]["seed"] for item in records}) != len(ids)):
        raise RegistryError("Light registry lights must be distinct.")
    devices = [_device_key(item["govee"]["device"]) for item in lights
               if item["provider"] == "govee"]
    if len(set(devices)) != len(devices):
        raise RegistryError("Light registry lights must be distinct.")
    migrated = {item["id"] for item in lights
                if item["provider"] == "hue" and item["id"] in _HUE_LIGHTS}
    if migrated not in (set(), set(_HUE_LIGHTS)):
        raise RegistryError("Light registry light set is unsupported.")
    try:
        return json.loads(json.dumps(root, allow_nan=False))
    except (TypeError, ValueError, RecursionError):
        raise _invalid() from None


def _device_key(value):
    """Compare Govee device IDs without regard to case or separators."""
    return value.replace(":", "").upper()


def _payload(data):
    """Encode a registry exactly as ``save`` writes it, so the size check matches."""
    return (json.dumps(data, indent=2) + "\n").encode()


def _append(data, light_id, provider, details):
    """Append a light; a light removed earlier gets its virtual identity back.

    A new light takes the lowest free virtual slot and its id as the identity seed.
    """
    retired = data.pop("retired", [])
    record = next((item for item in retired if item["id"] == light_id), None)
    if record is not None:
        retired.remove(record)
        virtual = record["virtual"]
    else:
        used = {int(item["virtual"]["id_v1"]) for item in data["lights"] + retired}
        virtual = {"id_v1": str(next(n for n in range(1, 10000) if n not in used)),
                   "seed": light_id}
    data["lights"].append({"id": light_id, "provider": provider, "virtual": virtual,
                           provider: details})
    if retired:
        data["retired"] = retired


def _profile(data):
    """Return the native Hue profile of registry data."""
    return NativeHueProfile.from_lights(
        data["hue"]["area_id"],
        [{"id": item["id"], "light": item["hue"]["light"], "channels": item["hue"]["channels"]}
         for item in data["lights"] if item["provider"] == "hue"])


@dataclass(frozen=True, slots=True, init=False)
class LightRegistry:
    """Validated, immutable list of the enrolled lights, free of credentials."""

    _json: str

    def __init__(self):
        raise RegistryError("Create light registries with from_dict or load.")

    @classmethod
    def from_dict(cls, data):
        """Validate every field, then retain only an immutable serialized copy."""
        normalized = _validated(data)
        if any(item["provider"] == "hue" for item in normalized["lights"]):
            try:
                _profile(normalized)
            except NativeProfileError:
                raise RegistryError("Light registry Hue resources are invalid.") from None
        if len(_payload(normalized)) > _MAX_REGISTRY_BYTES:
            raise RegistryError("Light registry exceeds the size limit.")
        registry = object.__new__(cls)
        object.__setattr__(registry, "_json", json.dumps(normalized))
        return registry

    @classmethod
    def load(cls, path):
        """Read at most 64 KiB of JSON, with sanitized errors and duplicate detection."""
        try:
            with Path(path).open("rb") as source:
                encoded = source.read(_MAX_REGISTRY_BYTES + 1)
            if len(encoded) > _MAX_REGISTRY_BYTES:
                raise RegistryError("Light registry exceeds the size limit.")
            data = json.loads(encoded.decode("utf-8"), object_pairs_hook=_unique_object)
        except RegistryError:
            raise
        except (OSError, ValueError, TypeError, RecursionError):
            raise RegistryError("Light registry could not be read.") from None
        return cls.from_dict(data)

    def to_dict(self):
        """Return a fresh deep copy; callers cannot change the registry."""
        return json.loads(self._json)

    @property
    def ids(self):
        return tuple(item["id"] for item in self.to_dict()["lights"])

    def govee_targets(self):
        """Return fresh adapter target dictionaries, each with its light id."""
        return [
            {"light": item["id"], **item["govee"]}
            for item in self.to_dict()["lights"] if item["provider"] == "govee"
        ]

    def with_govee_targets(self, targets):
        """Return a registry whose Govee lights take the given adapter targets.

        The adapter follows a lamp to a new address and keeps the lights, so the
        result must carry the same light ids; ``light`` itself is not stored.
        """
        if type(targets) is not list:
            raise _invalid()
        replacement = {}
        for target in targets:
            if type(target) is not dict or type(target.get("light")) is not str:
                raise _invalid()
            replacement[target["light"]] = {
                key: value for key, value in target.items() if key != "light"}
        data = self.to_dict()
        govee = [item for item in data["lights"] if item["provider"] == "govee"]
        if len(replacement) != len(targets) or set(replacement) != {i["id"] for i in govee}:
            raise _invalid()
        for item in govee:
            item["govee"] = replacement[item["id"]]
        return LightRegistry.from_dict(data)

    def with_govee_light(self, light_id, device, ip, model):
        """Return a registry that also drives the discovered Govee light ``light_id``.

        A light removed earlier gets its virtual identity back; a new one takes the
        lowest free virtual slot and its id as the identity seed.
        """
        if type(light_id) is not str or DEVICE_ID.match(light_id) is None:
            raise _invalid()
        if type(model) is not str or not model:
            raise _invalid()
        data = self.to_dict()
        if light_id in {item["id"] for item in data["lights"]}:
            raise RegistryError("Light is already enrolled.")
        if type(device) is str and any(
                item["provider"] == "govee"
                and _device_key(item["govee"]["device"]) == _device_key(device)
                for item in data["lights"]):
            raise RegistryError("Light is already enrolled.")
        _append(data, light_id, "govee", {"device": device, "ip": ip, "model": model})
        return LightRegistry.from_dict(data)

    def with_hue_light(self, light_id, area_id, light, service, segments, model):
        """Return a registry that also drives the discovered Hue light ``light_id``.

        The light is a member of the paired Entertainment area ``area_id`` with
        the entertainment ``service`` and ``segments`` channels. Its canonical
        channels follow the channels of the lights enrolled before it, so the
        migrated channels and their packet bytes stay as they are.
        """
        if type(light_id) is not str or DEVICE_ID.match(light_id) is None:
            raise _invalid()
        if type(segments) is not int or not 1 <= segments <= MAX_CHANNELS:
            raise _invalid()
        data = self.to_dict()
        if light_id in {item["id"] for item in data["lights"]}:
            raise RegistryError("Light is already enrolled.")
        hue = [item for item in data["lights"] if item["provider"] == "hue"]
        if any(item["hue"]["light"] == light for item in hue):
            raise RegistryError("Light is already enrolled.")
        if data.setdefault("hue", {"area_id": area_id})["area_id"] != area_id:
            raise RegistryError("Light is outside the paired Entertainment area.")
        first = sum(len(item["hue"]["channels"]) for item in hue)
        channels = [{"channel_id": first + index, "rid": service, "index": index}
                    for index in range(segments)]
        _append(data, light_id, "hue", {"light": light, "channels": channels, "model": model})
        return LightRegistry.from_dict(data)

    def without_light(self, light_id):
        """Return a registry without the discovered light ``light_id``.

        The light's virtual identity is kept for a later rejoin. The migrated
        lamps are not removed here: they still own the fixed layout rows. The
        canonical channels of the Hue lights enrolled after a removed Hue light
        move down, so the channels stay numbered from zero.
        """
        if type(light_id) is not str or DEVICE_ID.match(light_id) is None:
            raise _invalid()
        data = self.to_dict()
        item = next((item for item in data["lights"] if item["id"] == light_id), None)
        if item is None:
            raise RegistryError("Light is not enrolled.")
        data["lights"].remove(item)
        if item["provider"] == "hue":
            removed = {channel["channel_id"] for channel in item["hue"]["channels"]}
            hue = [other for other in data["lights"] if other["provider"] == "hue"]
            for other in hue:
                for channel in other["hue"]["channels"]:
                    channel["channel_id"] -= sum(1 for cid in removed
                                                 if cid < channel["channel_id"])
            if not hue:
                del data["hue"]
        retired = data.pop("retired", [])
        retired.append({"id": light_id, "virtual": item["virtual"]})
        data["retired"] = retired[-_MAX_RETIRED:]
        return LightRegistry.from_dict(data)

    def native_profile(self):
        """Return the validated native Hue profile, or None without Hue lights."""
        data = self.to_dict()
        if "hue" not in data:
            return None
        return _profile(data)

    def save(self, path):
        """Replace the registry atomically with owner-only permissions."""
        path = Path(path)
        payload = _payload(self.to_dict())
        temporary = None
        try:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                    dir=path.parent, prefix=".lights-", suffix=".tmp", delete=False) as stream:
                temporary = stream.name
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
            temporary = None
            descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError:
            raise RegistryError("Light registry could not be written.") from None
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass


def _read_json(path, message):
    try:
        with Path(path).open("rb") as source:
            encoded = source.read(_MAX_REGISTRY_BYTES + 1)
        if len(encoded) > _MAX_REGISTRY_BYTES:
            raise RegistryError(message)
        return json.loads(encoded.decode("utf-8"), object_pairs_hook=_unique_object)
    except (RegistryError, OSError, ValueError, TypeError, RecursionError):
        raise RegistryError(message) from None


def migrate(targets_file, native_profile_file):
    """Build the registry from the live targets file and the native Hue profile.

    This only reads: the old files are migration inputs and are never changed.
    """
    message = "Light registry migration needs the live targets file"
    if targets_file is None:
        raise RegistryError(message)
    data = _read_json(targets_file, message)
    targets = data.get("targets") if type(data) is dict else None
    if type(targets) is not list:
        raise RegistryError(message)
    sides = {side: light_id for light_id, side in LEGACY_SIDES.items()}
    # The legacy file always carries the two migrated lamps; anything else is not
    # an installation this migration understands.
    found = [target.get("side") if type(target) is dict else None for target in targets]
    if len(found) != len(sides) or set(found) != set(sides):
        raise RegistryError("Light registry migration found an unsupported target")
    lights = []
    for target in targets:
        if type(target) is not dict or target.get("side") not in sides:
            raise RegistryError("Light registry migration found an unsupported target")
        light_id = sides[target["side"]]
        virtual = _SEEDS[light_id]
        lights.append({
            "id": light_id, "provider": "govee",
            "virtual": {"id_v1": virtual[0], "seed": virtual[1]},
            "govee": {key: value for key, value in target.items() if key != "side"},
        })
    result = {"version": 1, "lights": lights}
    if native_profile_file is not None:
        try:
            profile = NativeHueProfile.load(native_profile_file)
        except NativeProfileError:
            raise RegistryError("Light registry migration needs a valid native Hue profile"
                                ) from None
        hue = {light.pop("id"): light for light in profile.to_lights()}
        for light_id in _HUE_LIGHTS:
            virtual = _SEEDS[light_id]
            lights.append({
                "id": light_id, "provider": "hue",
                "virtual": {"id_v1": virtual[0], "seed": virtual[1]},
                "hue": hue[light_id],
            })
        result["hue"] = {"area_id": profile.area_id}
    return LightRegistry.from_dict(result)


def ensure(state_dir, targets_file, native_profile_file):
    """Return the registry of a state directory, migrating once when it is missing.

    An existing registry is the only source: it is loaded strictly and the old
    files are never read. A missing registry is migrated in memory only; the
    caller saves it once every startup check has passed.
    """
    path = Path(state_dir) / REGISTRY_FILE
    if path.exists() or path.is_symlink():
        return LightRegistry.load(path)
    return migrate(targets_file, native_profile_file)
