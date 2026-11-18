"""Private, bounded persistence of observed native entertainment locations."""

import copy
import json
import math
import os
import stat
import tempfile
import threading
from pathlib import Path

_MAX_BYTES = 16 * 1024
_INVALID = "invalid native location metadata"
_WRITE_FAILED = "native location cache could not be saved"


def _number(value, minimum, maximum):
    return (
        type(value) in (int, float)
        and minimum <= value <= maximum
        and math.isfinite(value)
    )


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(_INVALID)
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError(_INVALID)


class NativeLocationCache:
    """Retain copied, observed rows for one area and four allowed services.

    Construction reads optional state and treats invalid or inaccessible state as
    empty. It never writes. ``merge`` persists changes before committing memory,
    retaining absent services and previously observed equalization factors.
    Saving syncs the file, renames it, then syncs its containing directory. An
    error after rename can leave a newer valid file while memory stays unchanged;
    callers must treat any save error as a failure before changing native state.
    The path must be dedicated to one instance; its lock serializes that instance.
    """

    def __init__(self, path, area_id, allowed_services):
        try:
            self._path = Path(path)
            allowed = tuple(allowed_services)
            if (
                type(area_id) is not str
                or not area_id
                or len(allowed) != 4
                or any(type(rid) is not str or not rid for rid in allowed)
                or len(set(allowed)) != 4
            ):
                raise ValueError(_INVALID)
            self._allowed = frozenset(allowed)
        except (TypeError, ValueError, OSError):
            raise ValueError(_INVALID) from None
        self._area_id = area_id
        self._lock = threading.Lock()
        self._locations = self._load()

    def snapshot(self):
        """Return independent location rows keyed by entertainment service ID."""
        with self._lock:
            return copy.deepcopy(self._locations)

    def merge(self, observed):
        """Validate observed rows and atomically save effective metadata changes."""
        with self._lock:
            validated = self._validate_observed(observed)
            merged = copy.deepcopy(self._locations)
            for rid, row in validated.items():
                previous = merged.get(rid, {})
                if "equalization_factor" not in row and "equalization_factor" in previous:
                    row["equalization_factor"] = previous["equalization_factor"]
                merged[rid] = row
            if merged == self._locations:
                return
            self._persist(merged)
            self._locations = merged

    def _validate_row(self, row):
        if type(row) is not dict or set(row) not in (
            {"service", "positions"},
            {"service", "positions", "equalization_factor"},
        ):
            raise ValueError(_INVALID)
        service = row["service"]
        if (
            type(service) is not dict
            or set(service) != {"rid", "rtype"}
            or type(service["rid"]) is not str
            or service["rid"] not in self._allowed
            or type(service["rtype"]) is not str
            or service["rtype"] != "entertainment"
        ):
            raise ValueError(_INVALID)
        positions = row["positions"]
        if type(positions) is not list or not 1 <= len(positions) <= 12:
            raise ValueError(_INVALID)
        for position in positions:
            if (
                type(position) is not dict
                or set(position) != {"x", "y", "z"}
                or any(not _number(value, -1, 1) for value in position.values())
            ):
                raise ValueError(_INVALID)
        if "equalization_factor" in row and not _number(row["equalization_factor"], 0, 1):
            raise ValueError(_INVALID)
        return copy.deepcopy(row)

    def _validate_observed(self, observed):
        if type(observed) is not dict or len(observed) > len(self._allowed):
            raise ValueError(_INVALID)
        validated = {}
        for rid, row in observed.items():
            if type(rid) is not str or rid not in self._allowed:
                raise ValueError(_INVALID)
            row = self._validate_row(row)
            if rid != row["service"]["rid"]:
                raise ValueError(_INVALID)
            validated[rid] = row
        return validated

    @staticmethod
    def _check_owned_file(info):
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid():
            raise OSError(_WRITE_FAILED)

    def _destination_identity(self):
        try:
            info = self._path.lstat()
        except FileNotFoundError:
            return None
        self._check_owned_file(info)
        return info.st_dev, info.st_ino

    def _load(self):
        descriptor = None
        try:
            # Check before opening to avoid blocking on non-regular destinations.
            identity = self._destination_identity()
            if identity is None:
                return {}
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            descriptor = os.open(self._path, flags)
            info = os.fstat(descriptor)
            self._check_owned_file(info)
            if (info.st_dev, info.st_ino) != identity or info.st_size > _MAX_BYTES:
                return {}
            with os.fdopen(descriptor, "rb") as source:
                descriptor = None
                raw = source.read(_MAX_BYTES + 1)
            if len(raw) > _MAX_BYTES:
                return {}
            data = json.loads(
                raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant
            )
            if (
                type(data) is not dict
                or set(data) != {"version", "area_id", "locations"}
                or type(data["version"]) is not int
                or data["version"] != 1
                or type(data["area_id"]) is not str
                or data["area_id"] != self._area_id
                or type(data["locations"]) is not list
                or len(data["locations"]) > len(self._allowed)
            ):
                return {}
            locations = {}
            for row in data["locations"]:
                row = self._validate_row(row)
                rid = row["service"]["rid"]
                if rid in locations:
                    return {}
                locations[rid] = row
            return locations
        except (OSError, ValueError, TypeError, RecursionError):
            return {}
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    @staticmethod
    def _ensure_directory(path):
        try:
            path.mkdir(mode=0o700)
        except FileNotFoundError:
            NativeLocationCache._ensure_directory(path.parent)
            path.mkdir(mode=0o700)
        except FileExistsError:
            if not path.is_dir():
                raise OSError(_WRITE_FAILED) from None

    def _persist(self, locations):
        data = {
            "version": 1,
            "area_id": self._area_id,
            "locations": [locations[rid] for rid in sorted(locations)],
        }
        try:
            encoded = (json.dumps(data, allow_nan=False, separators=(",", ":")) + "\n").encode()
        except (ValueError, TypeError, UnicodeError):
            raise ValueError(_INVALID) from None
        if len(encoded) > _MAX_BYTES:
            raise ValueError(_INVALID)
        descriptor = None
        directory_descriptor = None
        temporary = None
        try:
            identity = self._destination_identity()
            self._ensure_directory(self._path.parent)
            descriptor, temporary = tempfile.mkstemp(
                prefix="tmp_native_locations_", dir=self._path.parent
            )
            os.fchmod(descriptor, 0o600)
            remaining = memoryview(encoded)
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    raise OSError(_WRITE_FAILED)
                remaining = remaining[written:]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            if self._destination_identity() != identity:
                raise OSError(_WRITE_FAILED)
            os.replace(temporary, self._path)
            temporary = None
            directory_descriptor = os.open(
                self._path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            )
            os.fsync(directory_descriptor)
        except (OSError, ValueError):
            raise OSError(_WRITE_FAILED) from None
        finally:
            if directory_descriptor is not None:
                try:
                    os.close(directory_descriptor)
                except OSError:
                    pass
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
