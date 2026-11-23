"""Strict, immutable selection data for the native Hue lights of one Entertainment area.

The profile is a list of native Hue lights, each keyed by its registry light id,
with its native light and entertainment service UUIDs and its ordered channels.
Channel ids are canonical: they number the area's channels from zero, and a
light's channels follow its segment order. The light registry is the source; the
version-one profile file (four fixed roles, twelve channels) is read only as a
migration input.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

# Version-one role of each migrated Hue light.
_V1_ROLES = {"hue-monitor": "strip", "play-left": "hue_left",
             "play-center": "hue_center", "play-right": "hue_right"}
# Canonical channels of the version-one Play lights; the strip has channels 3-11.
_V1_PLAY_CHANNELS = {"play-center": 0, "play-left": 1, "play-right": 2}
_MAX_PROFILE_BYTES = 16 * 1024
# A Hue Entertainment area holds at most ten lights and twenty channels.
MAX_LIGHTS = 10
MAX_CHANNELS = 20


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


def _v1_lights(data):
    """Validate a version-one profile and return its area and registry light list."""
    roles = tuple(_V1_ROLES.values())
    root = _object(data, ("version", "lights", "entertainment"))
    if type(root["version"]) is not int or root["version"] != 1:
        raise NativeProfileError("Native Hue profile version is unsupported.")
    lights = _object(root["lights"], roles)
    entertainment = _object(root["entertainment"], ("area_id", "channels"))
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
        members[channel_id] = {"channel_id": channel_id, "rid": _uuid(member["rid"]),
                               "index": member["index"]}
    owned = {light_id: [members[cid]] for light_id, cid in _V1_PLAY_CHANNELS.items()}
    owned["hue-monitor"] = [members[cid] for cid in range(3, 12)]
    result = [{"id": light_id, "light": _uuid(lights[role]), "channels": owned[light_id]}
              for light_id, role in _V1_ROLES.items()]
    return _uuid(entertainment["area_id"]), result


@dataclass(frozen=True, slots=True, init=False)
class NativeHueProfile:
    """Validated list of native Hue lights and their channels, free of credentials."""

    area_id: str
    # (light id, native light UUID, service UUID, canonical channel ids), ordered
    # by first channel.
    _lights: tuple[tuple[str, str, str, tuple[int, ...]], ...]
    # Canonical channel id -> (service UUID, segment index).
    expected_members: tuple[tuple[str, int], ...]

    def __init__(self):
        raise NativeProfileError("Create native Hue profiles with from_lights or load.")

    @classmethod
    def from_lights(cls, area_id, lights):
        """Validate the area and each light, then retain only immutable values.

        ``lights`` holds ``{"id", "light", "channels": [{"channel_id", "rid",
        "index"}]}`` entries, as the light registry stores them.
        """
        area_id = _uuid(area_id)
        if type(lights) is not list or not 1 <= len(lights) <= MAX_LIGHTS:
            raise NativeProfileError("Native Hue profile lights are invalid.")
        members, entries = {}, []
        for light in lights:
            light = _object(light, ("id", "light", "channels"))
            if type(light["id"]) is not str or not light["id"]:
                raise NativeProfileError("Native Hue profile light IDs are invalid.")
            channels = light["channels"]
            if type(channels) is not list or not channels:
                raise NativeProfileError("Native Hue profile channels are invalid.")
            rows = []
            for channel in channels:
                channel = _object(channel, ("channel_id", "rid", "index"))
                channel_id = channel["channel_id"]
                if (type(channel_id) is not int or not 0 <= channel_id < MAX_CHANNELS
                        or channel_id in members or type(channel["index"]) is not int):
                    raise NativeProfileError("Native Hue profile channel IDs are invalid.")
                rows.append((channel_id, _uuid(channel["rid"]), channel["index"]))
                members[channel_id] = rows[-1][1:]
            rows.sort()
            services = {rid for _, rid, _ in rows}
            if len(services) != 1 or [index for _, _, index in rows] != list(range(len(rows))):
                raise NativeProfileError("Native Hue profile member indexes are invalid.")
            entries.append((light["id"], _uuid(light["light"]), services.pop(),
                            tuple(cid for cid, _, _ in rows)))
        if sorted(members) != list(range(len(members))):
            raise NativeProfileError("Native Hue profile channel IDs are invalid.")
        ids = [entry[0] for entry in entries]
        resources = [entry[1] for entry in entries] + [entry[2] for entry in entries]
        if len(set(ids)) != len(ids) or len({*resources, area_id}) != len(resources) + 1:
            raise NativeProfileError("Native Hue profile resources must be distinct.")
        profile = object.__new__(cls)
        object.__setattr__(profile, "area_id", area_id)
        object.__setattr__(profile, "_lights", tuple(sorted(entries, key=lambda e: e[3][0])))
        object.__setattr__(profile, "expected_members",
                           tuple(members[cid] for cid in range(len(members))))
        return profile

    @classmethod
    def from_dict(cls, data):
        """Read a version-one profile (four roles, twelve channels) as a migration input."""
        return cls.from_lights(*_v1_lights(data))

    @classmethod
    def load(cls, path):
        """Read a version-one profile file of at most 16 KiB, with sanitized errors."""
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
    def ids(self):
        """Registry ids of the native lights, ordered by their first channel."""
        return tuple(entry[0] for entry in self._lights)

    @property
    def light_ids(self):
        """Return ``{light id: native light UUID}``."""
        return {entry[0]: entry[1] for entry in self._lights}

    @property
    def service_ids(self):
        """Return ``{light id: entertainment service UUID}``."""
        return {entry[0]: entry[2] for entry in self._lights}

    @property
    def channels(self):
        """Return ``{light id: canonical channel ids in segment order}``."""
        return {entry[0]: entry[3] for entry in self._lights}

    @property
    def resource_ids(self):
        """Return only light resources that a light request may address."""
        return frozenset(entry[1] for entry in self._lights)

    def to_lights(self):
        """Return the registry form of the lights, in channel order."""
        return [
            {"id": light_id, "light": light,
             "channels": [{"channel_id": cid, "rid": service, "index": index}
                          for index, cid in enumerate(channels)]}
            for light_id, light, service, channels in self._lights
        ]
