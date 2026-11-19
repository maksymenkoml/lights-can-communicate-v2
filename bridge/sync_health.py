"""Verified Sync wait reasons and a bounded export without private identifiers.

Both functions only read an adapter ``/status`` dictionary. A reason is
reported only from the adapter's own classified transition or a fresh, healthy
reading; anything else stays ``unknown`` rather than being guessed.
"""

import math

from power_history import CAUSES as _POWER_CAUSES
from stream_diagnostics import _REASONS, _STATES

GOVEE_ROLES = ("left", "right")
HUE_ROLES = ("strip", "hue_left", "hue_center", "hue_right")
GOVEE_FRESH_SECONDS = 0.75
# Under DreamView a Govee lamp misses a few status replies in a row now and
# then. Within this time its last verified reading still stands.
GOVEE_HOLD_SECONDS = 2
HUE_FRESH_SECONDS = 2
FRAME_FRESH_SECONDS = 0.5
MAX_EVENTS = 64
MAX_TEXT = 120
_EVENT_METRICS = ("frames_received", "frames_sent", "forwarded", "failed", "frame_age",
                  "duration_ms", "consecutive_failures", "errno", "http_status")


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _text(value):
    return value[:MAX_TEXT] if type(value) is str else None


def _fresh(age, limit):
    return _number(age) and 0 <= age <= limit


def _selected(status):
    composition = status.get("composition")
    if composition is None:
        return set(GOVEE_ROLES + HUE_ROLES)
    roles = _mapping(composition).get("enabled_roles")
    if not isinstance(roles, list):
        return set()
    return {role for role in roles if role in GOVEE_ROLES + HUE_ROLES}


def _govee_rows(status):
    rows = {}
    targets = status.get("targets")
    for target in targets[:16] if isinstance(targets, list) else []:
        if isinstance(target, dict) and target.get("side") in GOVEE_ROLES:
            rows.setdefault(target["side"], target)
    return rows


def govee_reading(row):
    """Return a Govee row's fresh verified status, else a recently verified one."""
    native = _mapping(row.get("native"))
    if native.get("identity_verified") is True and _fresh(row.get("status_age"),
                                                          GOVEE_FRESH_SECONDS):
        return native
    held = _mapping(row.get("verified_native"))
    if held.get("identity_verified") is True and _fresh(row.get("verified_age"),
                                                        GOVEE_HOLD_SECONDS):
        return held
    return None


def _hue_rows(status):
    rows = {"strip": _mapping(status.get("hue_strip"))} if "hue_strip" in status else {}
    play = _mapping(status.get("hue_play"))
    rows.update({role: _mapping(play[role]) for role in HUE_ROLES[1:] if role in play})
    return rows


def _classified(video):
    events = _mapping(video.get("diagnostics")).get("events")
    if isinstance(events, list) and events and isinstance(events[-1], dict):
        state, reason = events[-1].get("state"), events[-1].get("reason")
        if state in _STATES and reason in _REASONS:
            return state, reason
    return "unknown", "unknown"


def _blockers(status, video):
    selected = _selected(status)
    blockers = []
    roles = _mapping(_mapping(status.get("power_owner")).get("roles"))
    govee = _govee_rows(status)
    for role in GOVEE_ROLES:
        if role not in selected:
            continue
        owner = _mapping(roles.get(role))
        if owner.get("requested_on") is False:
            blockers.append({"role": role, "code": "owner_off"})
        elif owner.get("requested_on") is True and owner.get("color_allowed") is False:
            blockers.append({"role": role, "code": "color_fenced"})
        if role in govee:
            native = govee_reading(govee[role])
            if native is None:
                blockers.append({"role": role, "code": "native_stale"})
            elif native.get("onOff") == 0 and type(native.get("onOff")) is int:
                blockers.append({"role": role, "code": "native_off"})
    owned = video.get("native_owned") is True
    for role, row in _hue_rows(status).items():
        if role not in selected:
            continue
        if row.get("healthy") is not True or not _fresh(row.get("native_age"), HUE_FRESH_SECONDS):
            blockers.append({"role": role, "code": "native_stale"})
            continue
        if row.get("native_on") is False:
            blockers.append({"role": role, "code": "native_off"})
        if row.get("native_mode") == "streaming" and not owned:
            blockers.append({"role": role, "code": "native_foreign"})
    return blockers


def derive(status):
    """Return the verified Sync health summary for one adapter status."""
    status = _mapping(status)
    video = _mapping(status.get("video"))
    state, reason = _classified(video)
    received = video.get("frames_received")
    age = video.get("frame_age")
    received = received if type(received) is int and received >= 0 else None
    age = age if _number(age) else None
    sent = video.get("frames_sent")
    return {
        "version": 1,
        "state": state,
        "reason": reason,
        "detail": _text(video.get("last_result")),
        "frames": {"received": received, "age": age,
                   "fresh": bool(received) and _fresh(age, FRAME_FRESH_SECONDS)},
        "output": {"ready": video.get("ready") is True,
                   "native_owned": video.get("native_owned") is True,
                   "sent": sent if type(sent) is int and sent >= 0 else None,
                   "cleanup_pending": video.get("cleanup_pending") is True},
        "blockers": _blockers(status, video),
    }


def _scalar_map(value, keys):
    value = _mapping(value)
    return {key: value[key] for key in keys
            if key in value and (value[key] is None or type(value[key]) in (bool, str)
                                 or _number(value[key]))
            and (type(value[key]) is not str or len(value[key]) <= MAX_TEXT)}


def _counters(value):
    return {key: count for key, count in _mapping(value).items()
            if (key in GOVEE_ROLES or key in _REASONS) and type(count) is int}


def _native_row(row):
    return _scalar_map(row, ("healthy", "native_on", "native_mode", "native_age",
                             "native_brightness", "last_result"))


def _power_events(authority):
    events = _mapping(authority).get("events")
    clean = []
    for item in events[-MAX_EVENTS:] if isinstance(events, list) else []:
        item = _mapping(item)
        if (item.get("role") in GOVEE_ROLES and type(item.get("cause")) is str
                and item["cause"] in _POWER_CAUSES
                and type(item.get("timestamp")) is str and len(item["timestamp"]) <= 40):
            clean.append({
                "timestamp": item["timestamp"], "role": item["role"], "cause": item["cause"],
                "requested_on": item.get("requested_on")
                if type(item.get("requested_on")) is bool else None,
                "color_allowed": item.get("color_allowed") is True,
                "sync_hold": item.get("sync_hold") is True,
            })
    return clean


def export(status):
    """Return a bounded diagnostic copy without credentials or device identifiers."""
    status = _mapping(status)
    video = _mapping(status.get("video"))
    events = _mapping(video.get("diagnostics")).get("events")
    clean_events = []
    for item in events[-MAX_EVENTS:] if isinstance(events, list) else []:
        item = _mapping(item)
        if item.get("state") in _STATES and item.get("reason") in _REASONS:
            clean_events.append({
                **_scalar_map(item, ("timestamp", "state", "reason", "elapsed")),
                "metrics": _scalar_map(item.get("metrics"), _EVENT_METRICS),
            })
    failure = _mapping(video.get("last_safety_failure"))
    natives = failure.get("native")
    govee = {}
    for role, target in _govee_rows(status).items():
        native = _mapping(target.get("native"))
        power = native.get("onOff")
        govee[role] = {
            "on": bool(power) if type(power) is int and power in (0, 1) else None,
            "brightness": native.get("brightness") if _number(native.get("brightness")) else None,
            "identity_verified": native.get("identity_verified") is True,
            "status_age": target.get("status_age") if _number(target.get("status_age")) else None,
            "segment_count": target.get("segment_count")
            if type(target.get("segment_count")) is int else None,
        }
    authority = _mapping(status.get("power_owner"))
    roles = _mapping(authority.get("roles"))
    return {
        "version": 1,
        "service": _scalar_map(status.get("service"), ("name", "version")),
        "sync": derive(status),
        "video": {
            **_scalar_map(video, ("active", "ready", "cleanup_pending", "native_owned",
                                  "frames_received", "frames_sent", "frame_age",
                                  "max_frame_gap", "sequence", "last_result")),
            "transitions": {key: count for key, count in _mapping(video.get("transitions")).items()
                            if key in _REASONS and type(count) is int},
            "events": clean_events,
            "last_safety_failure": {
                "phase": failure.get("phase") if failure.get("phase") in ("start", "stream")
                else None,
                "native": [_native_row(row) for row in natives[:8]]
                if isinstance(natives, list) else [],
            } if failure else None,
        },
        "power_owner": {role: _scalar_map(roles[role], ("requested_on", "color_allowed",
                                                        "revision", "grant_remaining"))
                        for role in GOVEE_ROLES if role in roles},
        "power_events": _power_events(authority),
        "govee": govee,
        "native": {role: _native_row(row) for role, row in _hue_rows(status).items()},
        "forwarding": {
            "commands_sent": status.get("commands_sent")
            if type(status.get("commands_sent")) is int else None,
            "rejected_sends": _counters(status.get("rejected_sends")),
            "skipped_state": _counters(status.get("skipped_state")),
        },
    }
