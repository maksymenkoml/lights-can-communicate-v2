"""Core-owned registry of the enrolled lights; the adapter reads only this file."""

import copy
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from .native_profile import NativeHueProfile, NativeProfileError

REGISTRY_FILE = "lights.json"
LEGACY_SIDES = {"govee-left": "left", "govee-right": "right"}
_MAX_REGISTRY_BYTES = 64 * 1024
_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")
_ID_V1 = re.compile(r"[1-9][0-9]{0,3}\Z")
_SEED = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
# Stage 1 supports the six lights of the current installation and nothing else.
_HUE_ROLES = {
    "hue-monitor": "strip",
    "play-left": "hue_left",
    "play-center": "hue_center",
    "play-right": "hue_right",
}


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
    return value


def _hue(value):
    value = _object(value, ("light", "channels"))
    _uuid(value["light"])
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
    _text(value["id"], _ID)
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
    if type(lights) is not list:
        raise _invalid()
    has_hue = any(type(item) is dict and item.get("provider") == "hue" for item in lights)
    root = _object(data, ("version", "lights", *(("hue",) if has_hue else ())))
    if type(root["version"]) is not int or root["version"] != 1:
        raise RegistryError("Light registry version is unsupported.")
    if has_hue:
        _uuid(_object(root["hue"], ("area_id",))["area_id"])
    for item in lights:
        _light(item)
    ids = [item["id"] for item in lights]
    if (len(set(ids)) != len(ids)
            or len({item["virtual"]["id_v1"] for item in lights}) != len(ids)
            or len({item["virtual"]["seed"] for item in lights}) != len(ids)):
        raise RegistryError("Light registry lights must be distinct.")
    govee = {item["id"] for item in lights if item["provider"] == "govee"}
    hue = {item["id"] for item in lights if item["provider"] == "hue"}
    if govee != set(LEGACY_SIDES) or hue not in (set(), set(_HUE_ROLES)):
        raise RegistryError("Light registry light set is unsupported.")
    try:
        return json.loads(json.dumps(root, allow_nan=False))
    except (TypeError, ValueError, RecursionError):
        raise _invalid() from None


def _profile_data(data):
    """Rebuild the version-one native profile dictionary from registry data."""
    hue = {item["id"]: item["hue"] for item in data["lights"] if item["provider"] == "hue"}
    channels = sorted(
        (
            {"channel_id": channel["channel_id"],
             "member": {"rid": channel["rid"], "index": channel["index"]}}
            for light in hue.values() for channel in light["channels"]
        ),
        key=lambda channel: channel["channel_id"],
    )
    return {
        "version": 1,
        "lights": {role: hue[light_id]["light"] for light_id, role in _HUE_ROLES.items()},
        "entertainment": {"area_id": data["hue"]["area_id"], "channels": channels},
    }


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
                NativeHueProfile.from_dict(_profile_data(normalized))
            except NativeProfileError:
                raise RegistryError("Light registry Hue resources are invalid.") from None
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
        """Return fresh adapter target dictionaries, each with its legacy side."""
        return [
            {"side": LEGACY_SIDES[item["id"]], **copy.deepcopy(item["govee"])}
            for item in self.to_dict()["lights"] if item["provider"] == "govee"
        ]

    def with_govee_targets(self, targets):
        """Return a registry whose Govee lights take the given adapter targets.

        The adapter follows a lamp to a new address and keeps the sides, so the
        result must carry the same two sides; ``side`` itself is not stored.
        """
        if type(targets) is not list:
            raise _invalid()
        sides = {side: light_id for light_id, side in LEGACY_SIDES.items()}
        replacement = {}
        for target in targets:
            if type(target) is not dict or target.get("side") not in sides:
                raise _invalid()
            replacement[sides[target["side"]]] = {
                key: value for key, value in target.items() if key != "side"}
        data = self.to_dict()
        govee = [item for item in data["lights"] if item["provider"] == "govee"]
        if len(replacement) != len(targets) or set(replacement) != {i["id"] for i in govee}:
            raise _invalid()
        for item in govee:
            item["govee"] = replacement[item["id"]]
        return LightRegistry.from_dict(data)

    def native_profile(self):
        """Return the validated native Hue profile, or None without Hue lights."""
        data = self.to_dict()
        if "hue" not in data:
            return None
        return NativeHueProfile.from_dict(_profile_data(data))

    def save(self, path):
        """Replace the registry atomically with owner-only permissions."""
        path = Path(path)
        payload = (json.dumps(self.to_dict(), indent=2) + "\n").encode()
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
            profile = NativeHueProfile.load(native_profile_file).to_dict()
        except NativeProfileError:
            raise RegistryError("Light registry migration needs a valid native Hue profile"
                                ) from None
        members = {item["channel_id"]: item["member"]
                   for item in profile["entertainment"]["channels"]}
        strip_service = members[3]["rid"]
        channels = {
            "hue-monitor": [cid for cid, member in members.items()
                            if member["rid"] == strip_service],
            "play-left": [1],
            "play-center": [0],
            "play-right": [2],
        }
        for light_id, role in _HUE_ROLES.items():
            virtual = _SEEDS[light_id]
            lights.append({
                "id": light_id, "provider": "hue",
                "virtual": {"id_v1": virtual[0], "seed": virtual[1]},
                "hue": {
                    "light": profile["lights"][role],
                    "channels": [{"channel_id": cid, "rid": members[cid]["rid"],
                                  "index": members[cid]["index"]}
                                 for cid in sorted(channels[light_id])],
                },
            })
        result["hue"] = {"area_id": profile["entertainment"]["area_id"]}
    return LightRegistry.from_dict(result)


def ensure(state_dir, targets_file, native_profile_file, *, write):
    """Return the registry of a state directory, migrating once when it is missing.

    An existing registry is the only source: it is loaded strictly and the old
    files are never read. With ``write=False`` a missing registry is migrated in
    memory only, so a configuration check leaves the state untouched.
    """
    path = Path(state_dir) / REGISTRY_FILE
    if path.exists() or path.is_symlink():
        return LightRegistry.load(path)
    registry = migrate(targets_file, native_profile_file)
    if write:
        registry.save(path)
    return registry
