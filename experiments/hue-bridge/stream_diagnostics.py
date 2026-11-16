"""Passive, bounded stream transitions without credentials or transport details."""

import json
import math
import os
import stat
import threading
import time
from collections import deque
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

_STATES = frozenset({"streaming", "waiting", "holding", "idle", "stopped", "unavailable"})
_REASONS = frozenset(
    {
        "frames_received",
        "transport_error",
        "transport_recovered",
        "frame_gap",
        "grace_elapsed",
        "explicit_stop",
        "native_unavailable",
        "native_off",
        "native_foreign",
        "relay_error",
        "shutdown",
        "starting",
    }
)
_METRICS = (
    "frames_received",
    "frames_sent",
    "forwarded",
    "failed",
    "frame_age",
    "duration_ms",
    "consecutive_failures",
    "errno",
    "http_status",
)
_MISSING = object()


def _numeric(value):
    # Exact built-in types exclude booleans and user objects with conversion hooks.
    return type(value) is int or (type(value) is float and math.isfinite(value))


def _read_clock(clock):
    try:
        value = clock()
        if _numeric(value) and math.isfinite(value):
            return value
    except Exception:
        pass
    return None


def _clean_metrics(metrics):
    clean = {}
    if isinstance(metrics, Mapping):
        for key in _METRICS:
            try:
                value = metrics.get(key, _MISSING)
                if value is None or _numeric(value):
                    clean[key] = value
            except Exception:
                continue
    return clean


class StreamDiagnostics:
    """Keep 64 copied transitions and optionally append private, rotating JSONL.

    ``events_written`` counts successful disk writes, while ``write_errors``
    counts failed attempts. Consecutive state/reason duplicates do neither.
    Missing parent directories are not created. One instance serializes its own
    records and snapshots; the optional path must be dedicated to that instance.
    """

    def __init__(self, path=None, clock=time.monotonic, wall_clock=time.time, max_bytes=262144):
        self._path = Path(path) if path is not None else None
        self._clock = clock
        self._wall_clock = wall_clock
        self._started = _read_clock(clock)
        self._max_bytes = max_bytes
        self._lock = threading.Lock()
        self._events = deque(maxlen=64)
        self._last = None
        self._events_written = 0
        self._write_errors = 0

    def record(self, state, reason, metrics=None):
        """Record only allowlisted data, without propagating diagnostic failures."""
        try:
            if type(state) is not str or type(reason) is not str:
                return
            if state not in _STATES or reason not in _REASONS:
                return
            with self._lock:
                if self._last == (state, reason):
                    return
                event = {
                    "timestamp": self._timestamp(),
                    "state": state,
                    "reason": reason,
                    "metrics": _clean_metrics(metrics),
                }
                now = _read_clock(self._clock)
                if now is not None and self._started is not None:
                    elapsed = now - self._started
                    if _numeric(elapsed) and math.isfinite(elapsed):
                        event["elapsed"] = elapsed
                self._events.append(event)
                self._last = (state, reason)
                if self._path is not None:
                    try:
                        self._write(event)
                    except Exception:
                        self._write_errors += 1
                    else:
                        self._events_written += 1
        except Exception:
            # A diagnostic must never interrupt stream handling or expose exceptions.
            return

    def snapshot(self):
        """Return independent event dictionaries under the same lock as writes."""
        with self._lock:
            return {
                "events": [{**event, "metrics": dict(event["metrics"])} for event in self._events],
                "events_written": self._events_written,
                "write_errors": self._write_errors,
            }

    def _timestamp(self):
        value = _read_clock(self._wall_clock)
        if value is not None:
            try:
                timestamp = datetime.fromtimestamp(value, timezone.utc)
                return timestamp.isoformat().replace("+00:00", "Z")
            except (OverflowError, OSError, ValueError):
                pass
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _check_owned_file(info):
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid():
            raise OSError("journal destination is not a dedicated owned regular file")

    def _open_private(self):
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open(self._path, flags, 0o600)
        try:
            self._check_owned_file(os.fstat(descriptor))
            os.fchmod(descriptor, 0o600)
        except Exception:
            os.close(descriptor)
            raise
        return descriptor

    def _write(self, event):
        line = (json.dumps(event, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")
        if len(line) > self._max_bytes:
            raise ValueError("journal event exceeds byte limit")
        descriptor = self._open_private()
        try:
            size = os.fstat(descriptor).st_size
            if size > self._max_bytes:
                raise ValueError("existing journal exceeds byte limit")
            if size + len(line) > self._max_bytes:
                backup = Path(str(self._path) + ".1")
                try:
                    self._check_owned_file(backup.lstat())
                except FileNotFoundError:
                    pass
                os.close(descriptor)
                descriptor = None
                os.replace(self._path, backup)
                descriptor = self._open_private()
                size = 0
            try:
                remaining = memoryview(line)
                while remaining:
                    written = os.write(descriptor, remaining)
                    if written <= 0:
                        raise OSError("journal write made no progress")
                    remaining = remaining[written:]
            except Exception:
                # Preserve JSONL framing if a failed append wrote only part of a row.
                try:
                    os.ftruncate(descriptor, size)
                except OSError:
                    pass
                raise
        finally:
            if descriptor is not None:
                os.close(descriptor)
