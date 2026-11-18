"""Strict, immutable selection data for the supported native Hue topology."""

import json
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

_ROLES = ("strip", "hue_left", "hue_center", "hue_right")
_MAX_PROFILE_BYTES = 16 * 1024


class NativeProfileError(ValueError):
    """A profile is invalid or unreadable; messages never include input values."""


def _object(value, fields):
    if type(value) is not dict or value.keys() != set(fields):
        raise NativeProfileError("Native Hue profile fields are invalid.")
    return value


def _uuid(value):
    if type(value) is not str:
        raise NativeProfileError("Native Hue profile resource UUID is invalid.")
    try:
        parsed = UUID(value)
    except ValueError:
        raise NativeProfileError("Native Hue profile resource UUID is invalid.") from None
    if parsed.int == 0:
        raise NativeProfileError("Native Hue profile resource UUID is invalid.")
    return str(parsed)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise NativeProfileError("Native Hue profile contains duplicate fields.")
        result[key] = value
    return result


@dataclass(frozen=True, slots=True, init=False)
class NativeHueProfile:
    """Validated four-light, twelve-channel profile containing no credentials."""

    _lights: tuple[str, str, str, str]
    area_id: str
    expected_members: tuple[tuple[str, int], ...]

    def __init__(self):
        raise NativeProfileError("Create native Hue profiles with from_dict or load.")

    @classmethod
    def from_dict(cls, data):
        """Validate every resource and mapping, then retain only immutable values."""
        root = _object(data, ("version", "lights", "entertainment"))
        if type(root["version"]) is not int or root["version"] != 1:
            raise NativeProfileError("Native Hue profile version is unsupported.")
        lights = _object(root["lights"], _ROLES)
        light_ids = tuple(_uuid(lights[role]) for role in _ROLES)
        entertainment = _object(root["entertainment"], ("area_id", "channels"))
        area_id = _uuid(entertainment["area_id"])
        if len(set((*light_ids, area_id))) != 5:
            raise NativeProfileError("Native Hue profile resources must be distinct.")
        channels = entertainment["channels"]
        if type(channels) is not list or len(channels) != 12:
            raise NativeProfileError("Native Hue profile must contain twelve channels.")
        members = {}
        for channel in channels:
            channel = _object(channel, ("channel_id", "member"))
            channel_id = channel["channel_id"]
            if type(channel_id) is not int or not 0 <= channel_id < 12 or channel_id in members:
                raise NativeProfileError("Native Hue profile channel IDs are invalid.")
            member = _object(channel["member"], ("rid", "index"))
            rid = _uuid(member["rid"])
            index = member["index"]
            expected_index = 0 if channel_id < 3 else channel_id - 3
            if type(index) is not int or index != expected_index:
                raise NativeProfileError("Native Hue profile member indexes are invalid.")
            members[channel_id] = (rid, index)
        ordered_members = tuple(members[channel_id] for channel_id in range(12))
        service_ids = tuple(ordered_members[channel_id][0] for channel_id in range(4))
        if len(set((*light_ids, area_id, *service_ids))) != 9:
            raise NativeProfileError("Native Hue profile resources must be distinct.")
        if any(member[0] != service_ids[3] for member in ordered_members[4:]):
            raise NativeProfileError("Native Hue profile strip membership is invalid.")
        profile = object.__new__(cls)
        object.__setattr__(profile, "_lights", light_ids)
        object.__setattr__(profile, "area_id", area_id)
        object.__setattr__(profile, "expected_members", ordered_members)
        return profile

    @classmethod
    def load(cls, path):
        """Read at most 16 KiB of JSON, with sanitized errors and duplicate detection."""
        try:
            with Path(path).open("rb") as source:
                encoded = source.read(_MAX_PROFILE_BYTES + 1)
            if len(encoded) > _MAX_PROFILE_BYTES:
                raise NativeProfileError("Native Hue profile exceeds the size limit.")
            data = json.loads(encoded.decode("utf-8"), object_pairs_hook=_unique_object)
        except NativeProfileError:
            raise
        except (OSError, ValueError, TypeError, RecursionError):
            raise NativeProfileError("Native Hue profile could not be read.") from None
        return cls.from_dict(data)

    @property
    def strip_id(self):
        return self._lights[0]

    @property
    def play_ids(self):
        return dict(zip(_ROLES[1:], self._lights[1:]))

    @property
    def light_ids(self):
        return dict(zip(_ROLES, self._lights))

    @property
    def resource_ids(self):
        """Return only light resources that a light request may address."""
        return frozenset(self._lights)

    def to_dict(self):
        """Return fresh containers in deterministic role and channel order."""
        return {
            "version": 1,
            "lights": self.light_ids,
            "entertainment": {
                "area_id": self.area_id,
                "channels": [
                    {"channel_id": channel_id, "member": {"rid": rid, "index": index}}
                    for channel_id, (rid, index) in enumerate(self.expected_members)
                ],
            },
        }
