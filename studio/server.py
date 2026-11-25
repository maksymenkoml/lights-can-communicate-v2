"""Loopback setup UI with validated desk geometry and calibration persistence."""

import argparse
import json
import math
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge"))
from desk_layout import LAYOUT_VERSION, ROLES  # noqa: E402
from hue_bridge_pairing import (  # noqa: E402
    BridgeError,
    Configured,
    HueBridgePairing,
    OtherBridge,
)
from hue_bridge_pairing import discover as discover_hue_bridges  # noqa: E402
from rooms import RoomRefused, Rooms  # noqa: E402
from sync_box import BoxError, SyncBox  # noqa: E402
from sync_box import discover as discover_sync_boxes  # noqa: E402
from sync_guard import SyncGuard  # noqa: E402
from sync_health import derive as derive_sync_health  # noqa: E402

STATIC = Path(__file__).resolve().parent / "static"
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    # Self-hosted Commissioner (SIL OFL 1.1, fonts/OFL.txt): no runtime font requests.
    "/fonts/commissioner-latin.woff2": ("fonts/commissioner-latin.woff2", "font/woff2"),
}
DEVICE_ID = re.compile(r"device-[a-f0-9]{32}\Z")
# Adapter refusal reasons passed through to the UI; anything else is "refused".
# Owner enrollment of a discovered light (adapter /lights/enroll and /lights/remove).
# Room management (#78): each route names an action of ``Rooms``.
ROOM_ROUTES = ("/api/rooms/create", "/api/rooms/rename", "/api/rooms/order",
               "/api/rooms/delete", "/api/rooms/move", "/api/rooms/sync-box")
# Changes that restart the virtual bridge or change the Entertainment area; they wait
# until Sync is off.
ROOM_CHANGES_OUTSIDE_SYNC = ("create", "delete", "move", "sync-box")
ROOM_REASONS = ("invalid_name", "room_limit", "unknown_room", "unknown_light", "last_room",
                "has_sync_box", "has_lights", "layout_full", "sync_active", "restarting",
                "unavailable")
LIGHT_ROUTES = {"/api/lights/enroll": "/lights/enroll", "/api/lights/remove": "/lights/remove"}
OUTSIDE_REASONS = ("room", "channels_shared", "area_full")
LIGHT_REASONS = ("sync_active", "restarting", "enrolled", "not_enrolled", "unavailable",
                 "area_full", "channels_shared")
IDENTIFY_REASONS = ("lamp off", "writer closed", "sync active", "busy", "unavailable",
                    "brightness unknown", "paused")
# Calibration refusals passed through to the UI; anything else is "refused".
CALIBRATION_REASONS = ("sync active", "lamp off", "reference off", "busy", "writer closed",
                       "paused")
CALIBRATION_ROUTES = {"/api/calibration": "/calibration",
                      "/api/calibration/end": "/calibration/end"}
HUE_BRIDGE_ROUTES = ("/api/hue-bridge/find", "/api/hue-bridge/pair",
                     "/api/hue-bridge/cancel")
SYNC_BOX_ROUTES = ("/api/sync-box/find", "/api/sync-box/pair", "/api/sync-box/cancel",
                   "/api/sync-box/sync", "/api/sync-box/forget")
CANONICAL_FIELDS = ("name", "model", "shape", "zones")
# Enrolled lights shown at once: the registry and layout limits stay below this.
MAX_LAMPS = 32
DISCOVERY_FIELDS = (
    "id", "provider", "name", "model", "shape", "zones", "supported", "online",
    "selectable", "ambiguous", "layout_id", "height_cm", "width_cm",
)


def public_catalog(data):
    """Project only bounded discovery metadata; adapter internals stay server-side."""
    devices = []
    for row in data.get("devices", [])[:128]:
        if not isinstance(row, dict) or not DEVICE_ID.fullmatch(str(row.get("id", ""))):
            continue
        if row.get("provider") not in ("govee", "hue"):
            continue
        if any(not isinstance(row.get(key), str) or not 1 <= len(row[key]) <= bound
               for key, bound in (("name", 80), ("model", 64))):
            continue
        if row.get("shape") not in ("floor", "strip", "bar", None):
            continue
        zones = row.get("zones")
        if zones is not None and (type(zones) is not int or not 1 <= zones <= 32):
            continue
        if any(type(row.get(key)) is not bool
               for key in ("supported", "online", "selectable", "ambiguous")):
            continue
        if row["supported"] and (row["shape"] is None or zones is None):
            continue
        if row.get("layout_id") is not None and not _light_id(row["layout_id"]):
            continue
        if any(row.get(key) is not None and
               (type(row[key]) not in (int, float) or not math.isfinite(row[key])
                or not 0 < row[key] <= 400) for key in ("height_cm", "width_cm")):
            continue
        devices.append({key: row.get(key) for key in DISCOVERY_FIELDS})
    providers = data.get("providers", {})
    return {
        "connected": data.get("connected") is True,
        "devices": devices,
        "providers": {
            key: providers.get(key) if providers.get(key) in
            ("discovering", "ready", "unavailable", "not_configured", "stale")
            else "unavailable" for key in ("govee", "hue")
        },
    }


def _number(value, low, high):
    return type(value) in (int, float) and low <= value <= high and math.isfinite(value)


def _fresh(value, limit):
    return _number(value, 0, limit)


def _rgb_colors(value):
    if (
        not isinstance(value, list)
        or not 1 <= len(value) <= 9
        or any(
            not isinstance(row, (list, tuple))
            or len(row) != 3
            or any(type(c) is not int or not 0 <= c <= 255 for c in row)
            for row in value
        )
    ):
        return []
    return [list(row) for row in value]


def _sample_zones(colors, count):
    if not colors or len(colors) == count:
        return colors
    if len(colors) == 1:
        return [list(colors[0]) for _ in range(count)]
    result = []
    for index in range(count):
        position = index * (len(colors) - 1) / max(1, count - 1)
        start = int(position)
        end = min(start + 1, len(colors) - 1)
        result.append([round(a + (b - a) * (position - start))
                       for a, b in zip(colors[start], colors[end])])
    return result


def _light_id(value):
    return isinstance(value, str) and (value in ROLES or DEVICE_ID.fullmatch(value) is not None)


def _registry_lights(value):
    """The enrolled lights that the adapter lists, bounded to UI metadata."""
    result = {}
    for row in (value if isinstance(value, list) else [])[:MAX_LAMPS]:
        if (not isinstance(row, dict) or not _light_id(row.get("id"))
                or row.get("provider") not in ("govee", "hue")
                or type(row.get("zones")) is not int or not 1 <= row["zones"] <= 32
                or type(row.get("migrated")) is not bool
                or not (row.get("model") is None or isinstance(row["model"], str)
                        and 1 <= len(row["model"]) <= 64)):
            continue
        result[row["id"]] = {key: row.get(key)
                             for key in ("id", "provider", "model", "zones", "migrated")}
        # A Hue light for scenes only, outside the Entertainment area, and why (#78).
        if row["provider"] == "hue" and row.get("outside") in OUTSIDE_REASONS:
            result[row["id"]]["outside"] = row["outside"]
    return result


def _unavailable(mode="unknown"):
    return {"version": 1, "connected": False, "mode": mode, "sync": {"state": mode}, "lamps": {},
            "lights": []}


def project_status(data, source_active=None):
    """Read-only, bounded UI projection of emitted colors and observed power."""
    if not isinstance(data, dict):
        return _unavailable()
    video = data.get("video") if isinstance(data.get("video"), dict) else {}
    preview = data.get("preview")
    if (
        not isinstance(preview, dict)
        or type(preview.get("version")) is not int
        or preview.get("version") != 1
    ):
        preview = {}
    preview_lamps = preview.get("lamps") if isinstance(preview.get("lamps"), dict) else {}
    lamps = {}
    tiers = {}
    rows = data.get("targets") if isinstance(data.get("targets"), list) else []
    for target in rows[:MAX_LAMPS]:
        if not isinstance(target, dict) or not _light_id(target.get("light")):
            continue
        native = target.get("native") if isinstance(target.get("native"), dict) else {}
        online = native.get("identity_verified") is True and _fresh(target.get("status_age"), 0.75)
        power = native.get("onOff")
        tiers[target["light"]] = target.get("tier")
        lamps[target["light"]] = {
            "online": online,
            "on": bool(power) if online and type(power) is int and power in (0, 1) else None,
            "brightness": native.get("brightness")
            if _number(native.get("brightness"), 0, 100)
            else None,
        }
    selected = data.get("hue") if isinstance(data.get("hue"), dict) else {}
    # Native Hue lamps by registry light id: the four migrated ones and enrolled ones.
    names = {lamp: lamp for lamp in ("hue-monitor", "play-left", "play-center", "play-right")}
    names.update({lamp: lamp for lamp in list(selected)[:MAX_LAMPS]
                  if _light_id(lamp) and lamp not in lamps})
    role_lamps = {'left': 'govee-left', 'right': 'govee-right',
                  **{lamp: lamp for lamp in ("hue-monitor", "play-left", "play-center",
                                             "play-right")}}
    registry = _registry_lights(data.get("lights"))
    composition = data.get('composition')
    included = set(role_lamps.values()) | set(lamps) | set(names) | set(registry)
    if composition is not None:
        selected_roles = (composition.get('enabled_roles')
                          if isinstance(composition, dict) else None)
        composed = composition.get('lights') if isinstance(composition, dict) else None
        included = ({role_lamps[role] for role in selected_roles}
                    if isinstance(selected_roles, list) and len(selected_roles) <= 6
                    and all(type(role) is str and role in role_lamps for role in selected_roles)
                    else set())
        # A light enrolled later is in Sync while its layout row is composed.
        if isinstance(composed, list):
            included |= {light for light in composed
                         if _light_id(light) and light not in ROLES}
    for role, identifier in names.items():
        state = selected.get(role) if isinstance(selected.get(role), dict) else {}
        online = state.get("healthy") is True and _fresh(state.get("native_age"), 2)
        lamps[identifier] = {
            "online": online,
            "on": state.get("native_on")
            if online and type(state.get("native_on")) is bool
            else None,
            "brightness": state.get("native_brightness")
            if _number(state.get("native_brightness"), 0, 100)
            else None,
        }
    emitted = (
        video.get("active") is True
        and video.get("ready") is True
        and _fresh(video.get("frame_age"), 0.5)
        and video.get("last_result", "streaming") == "streaming"
    )
    if preview and preview.get("mode") != "sync":
        emitted = False
    if emitted:
        sync_state, mode = "active", "sync"
    elif (preview.get("mode") == "foreign" and video.get("native_owned") is not True
          and video.get("cleanup_pending") is not True):
        sync_state = mode = "foreign"
    elif (video.get("active") is True or video.get("cleanup_pending") is True
          or video.get("ready") is True or video.get("native_owned") is True
          or preview.get("mode") == "waiting"):
        sync_state = mode = "waiting"
    elif preview.get("mode") == "foreign" or any(
        isinstance(selected.get(role), dict)
        and identifier in included
        and selected[role].get("native_mode") == "streaming"
        and lamps[identifier]["online"]
        and lamps[identifier]["on"] is not None
        for role, identifier in names.items()
    ):
        sync_state = mode = "foreign"
    elif source_active is True:
        sync_state = mode = "waiting"
    elif source_active is False:
        sync_state, mode = "off", "scene"
    else:
        sync_state = mode = "unknown"
    # The adapter lists the registry lights: exactly those are shown, in registry
    # order. An older adapter without the list shows the six migrated lamps first.
    known = (list(registry) if isinstance(data.get("lights"), list)
             else [*ROLES, *sorted(set(lamps) - set(ROLES))])[:MAX_LAMPS]
    for identifier in known:
        lamp = lamps.setdefault(identifier, {"online": False, "on": None, "brightness": None})
        row = preview_lamps.get(identifier)
        row = row if isinstance(row, dict) else {}
        source = (
            row.get("source") if row.get("source") in ("scene-output", "sync-output") else "unknown"
        )
        stale = (row.get("stale") is not False or not lamp["online"]
                 or row.get("online") is not True or row.get("on") is not True
                 or identifier not in included)
        colors = _rgb_colors(row.get("colors"))
        if (
            lamp["on"] is not True
            or stale
            or source == "unknown"
            or source == "sync-output"
            and not emitted
            or mode == "foreign"
            and identifier in names.values()
        ):
            colors = []
        # A segmented Govee lamp shows seven zones; a basic-tier lamp is one color. A
        # light enrolled later shows the zone count that the adapter reports for it.
        count = (9 if identifier == "hue-monitor"
                 else 1 if tiers.get(identifier) == "basic"
                 else 7 if identifier.startswith("govee-")
                 else registry.get(identifier, {}).get("zones", 1))
        lamp.update(colors=_sample_zones(colors, count), source=source, stale=stale,
                    included=identifier in included)
    # Verified cause only; the existing state label keeps its meaning.
    health = derive_sync_health(data)
    blockers = [item for item in health["blockers"] if item.get("role") in lamps]
    return {
        "version": 1,
        "connected": True,
        "mode": mode,
        "sync": {"state": sync_state, "reason": health["reason"],
                 "blockers": blockers},
        "lamps": {identifier: lamps[identifier] for identifier in known},
        "lights": list(registry.values()),
    }


class SourceActivityCache:
    """Single-flight receiver metadata, independent from physical device IO."""

    def __init__(self, fetch, clock=time.monotonic, ttl=1):
        self.fetch, self.clock, self.ttl = fetch, clock, ttl
        self.lock = threading.Lock()
        self.next_refresh = float("-inf")
        self.value = None

    def get(self):
        with self.lock:
            now = self.clock()
            if now >= self.next_refresh:
                try:
                    value = self.fetch()
                    self.value = value if type(value) is bool else None
                except Exception as error:
                    if isinstance(error, urllib.error.HTTPError):
                        error.close()
                    self.value = None
                self.next_refresh = now + self.ttl
            return self.value


class _NoLocalRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _read_local_json(url, timeout, headers=None):
    request = urllib.request.Request(url, headers=headers or {})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoLocalRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(262145)
        if len(raw) > 262144:
            raise ValueError("Oversize local status")
        return json.loads(raw)
    except urllib.error.HTTPError as error:
        error.close()
        raise


def _asset(name):
    """Read a static asset from a source tree or from the executable archive."""
    path = STATIC / name
    loader = globals().get("__loader__")
    if not path.is_file() and hasattr(loader, "get_data"):
        # An archive member key, independent of symlinks in the archive path.
        return loader.get_data("static/" + name)
    return path.read_bytes()


def _post_local_json(url, body, timeout, headers):
    """POST to the loopback adapter; return (status, JSON body) including refusals."""
    request = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", **headers})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoLocalRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, json.loads(response.read(4096))
    except urllib.error.HTTPError as error:
        with error:
            try:
                return error.code, json.loads(error.read(4096))
            except ValueError:
                return error.code, {}


def handler_for(token_file, state_dir=None, catalog_fetch=None, live_fetch=None,
                source_fetch=None, adapter_url="http://127.0.0.1:19880",
                bridge_url="http://127.0.0.1", identify_post=None, calibration_post=None,
                sync_box=None, hosts=(), lights_post=None, hue_bridge=None,
                hue_config=None):
    state_dir = Path(state_dir) if state_dir is not None else Path(token_file).parent
    rooms = Rooms(state_dir)

    def registry_lights(provider=None):
        """The ids of the connected lights (the light registry), of ``provider`` only
        when it is given; None when the registry cannot be read (no registry: none)."""
        try:
            data = json.loads((state_dir / "lights.json").read_text())
        except FileNotFoundError:
            return []
        except (OSError, ValueError):
            return None
        items = data.get("lights") if isinstance(data, dict) else None
        # Malformed contents prove nothing about the connected lights.
        if not isinstance(items, list) or any(
                not isinstance(item, dict) or not _light_id(item.get("id"))
                or item.get("provider") not in ("govee", "hue") for item in items):
            return None
        return [item["id"] for item in items if provider in (None, item["provider"])]

    def rooms_payload():
        index = rooms.index()
        names, placed = {}, {}
        for room in index["rooms"]:
            store = rooms.store(room)
            data = store.snapshot() if store is not None else None
            names[room] = data.get("room") if isinstance(data, dict) else None
            # Where each light's row is, so Found lists only lights that no room shows.
            for row in data.get("devices", ()) if isinstance(data, dict) else ():
                if isinstance(row, dict) and _light_id(row.get("id")):
                    placed.setdefault(row["id"], room)
        return {"rooms": [{"id": room, "name": names[room]} for room in index["rooms"]],
                "sync": index["sync"],
                "lights": {light: rooms.room_of(light, index)
                           for light in registry_lights() or ()},
                # Every recorded room of a light; any other light is in the default room.
                "assigned": index["lights"],
                "default": index["default"],
                "placed": placed}

    # The Sync Box token lives with the other private state, never in the layout.
    def lan_search(path, local):
        """Addresses that the adapter finds by mDNS on the LAN; without the adapter,
        this process searches itself (a source run on the LAN)."""
        def find():
            try:
                token = token_file.read_text().strip()
                answer = _read_local_json(adapter_url + path, 5, {"X-Adapter-Token": token})
                addresses = answer.get("addresses") if isinstance(answer, dict) else None
                if isinstance(addresses, list):
                    return [item for item in addresses if isinstance(item, str)][:16]
            except (OSError, ValueError):
                pass
            return local()
        return find

    box = sync_box or SyncBox(state_dir / "sync-box.json",
                              find=lan_search("/lan/sync-boxes", discover_sync_boxes))

    def has_hue_lights():
        """True when the light registry holds a Hue light (of the pinned bridge)."""
        try:
            data = json.loads((state_dir / "lights.json").read_text())
            return any(isinstance(item, dict) and item.get("provider") == "hue"
                       for item in data.get("lights", ()))
        except (OSError, ValueError, AttributeError, TypeError):
            # An unreadable registry cannot prove that no Hue light depends on the pin.
            return True

    # The paired bridge keys live with the other private state; the adapter restarts
    # when the file changes and then uses this bridge.
    bridge = hue_bridge or HueBridgePairing(state_dir / "hue-bridge.json",
                                            state_dir / "hue-bridge-id",
                                            has_hue_lights=has_hue_lights,
                                            configured=hue_config,
                                            find=lan_search("/lan/hue-bridges",
                                                            discover_hue_bridges))

    def default_source_fetch():
        data = _read_local_json(bridge_url + "/probe/status", 0.25)
        receiver = data.get("entertainment") if isinstance(data, dict) else None
        return receiver.get("active") if isinstance(receiver, dict) else None

    source_cache = SourceActivityCache(source_fetch or default_source_fetch)

    def live():
        if (state_dir / "live-output-hold").exists():
            return _unavailable("paused")
        try:
            if live_fetch is not None:
                data = live_fetch()
            else:
                token = token_file.read_text().strip()
                data = _read_local_json(
                    adapter_url + "/status", 0.5, {"X-Adapter-Token": token}
                )
            result = project_status(data)
            if not result["connected"] or result["sync"]["state"] != "unknown":
                return result
            return project_status(data, source_active=source_cache.get())
        except Exception as error:
            if isinstance(error, urllib.error.HTTPError):
                error.close()
            return _unavailable()


    def discovery():
        try:
            if catalog_fetch is not None:
                return public_catalog(catalog_fetch())
            token = token_file.read_text().strip()
            request = urllib.request.Request(
                adapter_url + "/discovery", headers={"X-Adapter-Token": token}
            )
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(request, timeout=1) as response:
                return public_catalog(json.load(response))
        except Exception as error:
            if isinstance(error, urllib.error.HTTPError):
                error.close()
            return {"connected": False, "devices": [],
                    "providers": {"govee": "unavailable", "hue": "unavailable"}}

    def identify(role):
        """Forward an owner Blink to the adapter; return (status, public body)."""
        if (state_dir / "live-output-hold").exists():
            return 409, {"accepted": False, "reason": "paused"}
        try:
            if identify_post is not None:
                status, body = identify_post(role)
            else:
                token = token_file.read_text().strip()
                # A Govee blink takes about 1.2 s before the adapter answers.
                status, body = _post_local_json(adapter_url + "/identify", {"role": role}, 3,
                                                 {"X-Adapter-Token": token})
        except Exception as error:
            if isinstance(error, urllib.error.HTTPError):
                error.close()
            return 503, {"accepted": False, "reason": "adapter unavailable"}
        reason = body.get("reason") if isinstance(body, dict) else None
        if status == 200:
            return 200, {"accepted": True}
        if status == 409:
            return 409, {"accepted": False,
                         "reason": reason if reason in IDENTIFY_REASONS else "refused"}
        return 503, {"accepted": False, "reason": "adapter unavailable"}

    def calibrate(route, body):
        """Forward a calibration request to the adapter; return (status, public body)."""
        if (state_dir / "live-output-hold").exists():
            return 409, {"active": False, "reason": "paused"}
        try:
            if calibration_post is not None:
                status, answer = calibration_post(route, body)
            else:
                token = token_file.read_text().strip()
                status, answer = _post_local_json(adapter_url + route, body, 2,
                                                  {"X-Adapter-Token": token})
        except Exception as error:
            if isinstance(error, urllib.error.HTTPError):
                error.close()
            return 503, {"active": False, "reason": "adapter unavailable"}
        if status == 200 and isinstance(answer, dict):
            return 200, {"active": answer.get("active") is True}
        if status == 409:
            reason = answer.get("reason") if isinstance(answer, dict) else None
            return 409, {"active": False,
                         "reason": reason if reason in CALIBRATION_REASONS else "refused"}
        if status == 400:
            return 400, {"error": "invalid calibration request"}
        return 503, {"active": False, "reason": "adapter unavailable"}

    def adapter_post(route, body):
        if lights_post is not None:
            return lights_post(route, body)
        token = token_file.read_text().strip()
        return _post_local_json(adapter_url + route, body, 10, {"X-Adapter-Token": token})

    def change_lights(route, light_id):
        """Forward an owner enrollment or removal to the adapter; return (status, body)."""
        try:
            status, answer = adapter_post(route, {"id": light_id})
        except Exception as error:
            if isinstance(error, urllib.error.HTTPError):
                error.close()
            return 503, {"accepted": False, "reason": "adapter unavailable"}
        if status == 200 and isinstance(answer, dict) and answer.get("accepted") is True:
            # The adapter restarts with the new light set. An enrollment names the
            # registry id: a migrated lamp that rejoins gets its migrated id back.
            result = {"accepted": True, "restarting": True}
            if _light_id(answer.get("id")):
                result["id"] = answer["id"]
            return 200, result
        if status == 409:
            reason = answer.get("reason") if isinstance(answer, dict) else None
            return 409, {"accepted": False,
                         "reason": reason if reason in LIGHT_REASONS else "refused"}
        if status == 400:
            return 400, {"error": "invalid light request"}
        return 503, {"accepted": False, "reason": "adapter unavailable"}

    def sync_busy():
        """True while the adapter's Sync output owns the lights or releases them, None
        when that is unknown."""
        try:
            if live_fetch is not None:
                data = live_fetch()
            else:
                token = token_file.read_text().strip()
                data = _read_local_json(adapter_url + "/status", 0.5,
                                        {"X-Adapter-Token": token})
        except Exception as error:
            if isinstance(error, urllib.error.HTTPError):
                error.close()
            return None
        if not isinstance(data, dict):
            return None
        # An adapter without Sync output lists no video state.
        video = data.get("video")
        if video is None:
            return False
        return isinstance(video, dict) and any(
            video.get(key) is True for key in ("active", "cleanup_pending", "native_owned"))

    def follow_rooms():
        """Let the adapter move the Hue lights into or out of the area; return None or
        a refusal (status, body)."""
        try:
            status, answer = adapter_post("/lights/rooms", {})
        except Exception as error:
            if isinstance(error, urllib.error.HTTPError):
                error.close()
            return 503, {"accepted": False, "reason": "unavailable"}
        if status == 200 and isinstance(answer, dict) and answer.get("accepted") is True:
            return None
        reason = answer.get("reason") if isinstance(answer, dict) else None
        return (409 if status == 409 else 503), {
            "accepted": False, "reason": reason if reason in ROOM_REASONS else "unavailable"}

    def change_rooms(action, data):
        """Apply a room change; return (status, body)."""
        if action in ROOM_CHANGES_OUTSIDE_SYNC:
            busy = sync_busy()
            if busy is None:
                # Without the adapter's state such a change could meet a running Sync.
                return 503, {"accepted": False, "reason": "unavailable"}
            if busy:
                return 409, {"accepted": False, "reason": "sync_active"}
        hue = registry_lights("hue") if action == "move" else ()
        if hue is None:
            # An unreadable registry cannot tell whether the area must follow.
            return 503, {"accepted": False, "reason": "unavailable"}
        with rooms.lock:
            before = rooms.state()
            try:
                if action == "create":
                    rooms.create(data["name"])
                elif action == "rename":
                    rooms.rename(data["room"], data["name"])
                elif action == "order":
                    rooms.reorder(data["rooms"])
                elif action == "delete":
                    connected = registry_lights()
                    if connected is None:
                        # An unreadable registry cannot prove that the room is empty.
                        return 503, {"accepted": False, "reason": "unavailable"}
                    rooms.delete(data["room"], connected)
                elif action == "move":
                    rooms.move(data["light"], data["room"])
                else:
                    rooms.place_sync_box(data["room"])
            except RoomRefused as refused:
                return 409, {"accepted": False, "reason": refused.reason}
            # The Hue lights follow their rooms into or out of the area; a Govee light
            # has no area, so its move needs no adapter (which may be restarting).
            follow = action == "sync-box" or (
                action == "move" and data["light"] in hue)
            refusal = follow_rooms() if follow else None
            if refusal is not None:
                rooms.restore(before)
                return refusal
        return 200, {"accepted": True, **rooms_payload()}

    def stream_state():
        sync = live().get("sync")
        return sync.get("state") if isinstance(sync, dict) else None

    def bridge_frames():
        data = _read_local_json(bridge_url + "/probe/status", 0.5)
        receiver = data.get("entertainment") if isinstance(data, dict) else None
        frames = receiver.get("frames_received") if isinstance(receiver, dict) else None
        return frames if type(frames) is int else None

    # Restarts Sync on the box after a bridge restart or a stall (see sync_guard).
    guard = SyncGuard(box, stream_state, bridge_frames)

    def box_status():
        return {**box.status(), **guard.public()}

    def set_sync(active):
        code, status = box.set_sync(active)
        if code == 200:
            (guard.started if active else guard.stopped)()
        return code, {**status, **guard.public()}

    def validate_bindings(data, store):
        if not isinstance(data, dict) or not isinstance(data.get("devices"), list):
            raise ValueError("Expected layout devices")
        previous = {row["id"]: row for row in (store.snapshot() or {}).get("devices", [])}
        catalog = None
        seen = set()
        for row in data.get("devices", []):
            if not isinstance(row, dict):
                raise ValueError("Expected placement")
            identifier = row.get("id")
            if identifier in seen:
                raise ValueError("Duplicate placement")
            seen.add(identifier)
            if not isinstance(identifier, str) or not DEVICE_ID.fullmatch(identifier):
                continue
            canonical = previous.get(identifier)
            if canonical is None:
                if catalog is None:
                    snapshot = discovery()
                    catalog = {item["id"]: item for item in snapshot["devices"]}
                    if not snapshot["connected"]:
                        raise ValueError("Discovery unavailable")
                canonical = catalog.get(identifier)
                if (not canonical or canonical.get("selectable") is not True
                        or canonical.get("online") is not True
                        or canonical.get("supported") is not True
                        or canonical.get("ambiguous") is not False
                        or canonical.get("layout_id") is not None):
                    raise ValueError("Unavailable placement")
            if any(row.get(key) != canonical.get(key) for key in CANONICAL_FIELDS):
                raise ValueError("Invalid canonical placement metadata")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, status, body, kind="application/json; charset=utf-8"):
            if not isinstance(body, bytes):
                body = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self.local_request():
                return self.respond(403, {"error": "local origin required"})
            path = urlsplit(self.path).path
            if path in ASSETS:
                name, kind = ASSETS[path]
                return self.respond(200, _asset(name), kind)
            if path == "/api/layout":
                store = self.room_store()
                if store is None:
                    return self.respond(404, {"error": "unknown room"})
                return self.respond(200, store.response())
            if path == "/api/rooms":
                return self.respond(200, rooms_payload())
            if path == "/api/discovery":
                return self.respond(200, discovery())
            if path == "/api/live":
                return self.respond(200, live())
            if path == "/api/sync-box":
                return self.respond(200, box_status())
            if path == "/api/hue-bridge":
                return self.respond(200, bridge.status())
            self.respond(404, {"error": "not found"})

        def do_POST(self):
            path = urlsplit(self.path).path
            if path not in ("/api/layout", "/api/identify", *CALIBRATION_ROUTES,
                            *SYNC_BOX_ROUTES, *HUE_BRIDGE_ROUTES, *LIGHT_ROUTES,
                            *ROOM_ROUTES):
                return self.respond(405, {"error": "unsupported write route"})
            if not self.local_request():
                return self.respond(403, {"error": "local origin required"})
            if self.headers.get_content_type() != "application/json":
                return self.respond(415, {"error": "JSON required"})
            if path == "/api/identify":
                return self.identify()
            if path in CALIBRATION_ROUTES:
                return self.calibrate(CALIBRATION_ROUTES[path])
            if path in SYNC_BOX_ROUTES:
                return self.sync_box(path.rsplit("/", 1)[1])
            if path in HUE_BRIDGE_ROUTES:
                return self.hue_bridge(path.rsplit("/", 1)[1])
            if path in LIGHT_ROUTES:
                return self.lights(LIGHT_ROUTES[path])
            if path in ROOM_ROUTES:
                return self.rooms(path.rsplit("/", 1)[1])
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 16384:
                    return self.respond(413, {"error": "layout payload too large"})
                data = json.loads(self.rfile.read(size))
                with rooms.lock:
                    store = self.room_store()
                    if store is None:
                        return self.respond(404, {"error": "unknown room"})
                    # Every write names the revision it edits ("" when none is saved), so
                    # a page loaded before an upgrade or another edit cannot overwrite it.
                    expected = self.headers.get("If-Match")
                    if expected is None:
                        return self.respond(428, {"error": "If-Match required"})
                    if expected.strip('"') != (store.revision() or ""):
                        return self.respond(409, {"error": "layout changed"})
                    # Older formats are migrated when the stored file is read, never
                    # accepted from a write.
                    if not isinstance(data, dict) or data.get("version") != LAYOUT_VERSION:
                        raise ValueError("Writes must use the current layout version")
                    previous = store.snapshot()
                    for zone in ('syncZone', 'sceneZone'):
                        if (isinstance(data, dict) and zone not in data
                                and previous and zone in previous):
                            # A stale client cannot silently erase a newer custom zone.
                            # New clients send explicit null to return to automatic.
                            data = {**data, zone: previous[zone]}
                    validate_bindings(data, store)
                    result = rooms.save(self.room_id(), data)
                return self.respond(200, result)
            except (ValueError, TypeError, KeyError):
                return self.respond(400, {"error": "invalid desk layout"})
            except OSError:
                return self.respond(503, {"error": "layout storage unavailable"})

        def room_id(self):
            """The room that ``?room=`` names, or the first room."""
            values = parse_qs(urlsplit(self.path).query).get("room")
            return values[0] if values else rooms.index()["rooms"][0]

        def room_store(self):
            return rooms.store(self.room_id())

        def rooms(self, action):
            fields = {"create": {"name"}, "rename": {"room", "name"}, "order": {"rooms"},
                      "delete": {"room"}, "move": {"light", "room"},
                      "sync-box": {"room"}}[action]
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 1024:
                    return self.respond(413, {"error": "room payload too large"})
                data = json.loads(self.rfile.read(size))
                if not isinstance(data, dict) or set(data) != fields:
                    raise ValueError("Expected the room fields")
            except (ValueError, TypeError):
                return self.respond(400, {"error": "invalid room request"})
            try:
                return self.respond(*change_rooms(action, data))
            except (ValueError, TypeError):
                # A rooms layout that a room's rows no longer fit.
                return self.respond(400, {"error": "invalid room request"})
            except OSError:
                return self.respond(503, {"error": "room storage unavailable"})

        def identify(self):
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 1024:
                    return self.respond(413, {"error": "identify payload too large"})
                data = json.loads(self.rfile.read(size))
                if (not isinstance(data, dict) or set(data) != {"lamp"}
                        or not isinstance(data["lamp"], str)):
                    raise ValueError("Expected a lamp")
            except (ValueError, TypeError):
                return self.respond(400, {"error": "invalid identify request"})
            # The adapter takes a registry light id and refuses a light it does not drive.
            role = data["lamp"] if _light_id(data["lamp"]) else None
            if role is None:
                return self.respond(404, {"error": "unknown lamp"})
            return self.respond(*identify(role))

        def lights(self, route):
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 256:
                    return self.respond(413, {"error": "light payload too large"})
                data = json.loads(self.rfile.read(size))
                # Only a discovered light is enrolled; any enrolled light, a migrated
                # one too, can be removed.
                known = (_light_id if route == "/lights/remove"
                         else lambda value: DEVICE_ID.fullmatch(value) is not None)
                # An enrollment may name the room the light joins (#78).
                fields = ({"id"}, {"id", "room"}) if route == "/lights/enroll" else ({"id"},)
                if (not isinstance(data, dict) or set(data) not in fields
                        or not isinstance(data["id"], str) or not known(data["id"])):
                    raise ValueError("Expected a light id")
            except (ValueError, TypeError):
                return self.respond(400, {"error": "invalid light request"})
            if "room" not in data:
                return self.respond(*change_lights(route, data["id"]))
            with rooms.lock:
                before = rooms.state()
                try:
                    # The adapter reads the light's room to decide its area membership.
                    rooms.place(data["id"], data["room"])
                except RoomRefused as refused:
                    return self.respond(409, {"accepted": False, "reason": refused.reason})
                status, answer = change_lights(route, data["id"])
                if status != 200:
                    rooms.restore(before)
                elif answer.get("id", data["id"]) != data["id"]:
                    # A migrated lamp that rejoins keeps its migrated id.
                    rooms.place(answer["id"], data["room"])
            return self.respond(status, answer)

        def calibrate(self, route):
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 1024:
                    return self.respond(413, {"error": "calibration payload too large"})
                data = json.loads(self.rfile.read(size))
                if not isinstance(data, dict):
                    raise ValueError("Expected a calibration object")
            except (ValueError, TypeError):
                return self.respond(400, {"error": "invalid calibration request"})
            return self.respond(*calibrate(route, data))

        def sync_box(self, action):
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 256:
                    return self.respond(413, {"error": "Sync Box payload too large"})
                data = json.loads(self.rfile.read(size))
                if not isinstance(data, dict):
                    raise ValueError("Expected an object")
                if action == "pair":
                    if set(data) != {"ip"}:
                        raise ValueError("Expected an address")
                    return self.respond(200, box.pair(data["ip"]))
                if action == "sync":
                    if set(data) != {"active"} or not isinstance(data["active"], bool):
                        raise ValueError("Expected active")
                    return self.respond(*set_sync(data["active"]))
                if data:
                    raise ValueError("Expected an empty object")
                return self.respond(200, {"find": box.find, "cancel": box.cancel,
                                          "forget": box.forget}[action]())
            except BoxError:
                return self.respond(503, {"error": "Sync Box not found"})
            except (ValueError, TypeError):
                return self.respond(400, {"error": "invalid Sync Box request"})
            except OSError:
                return self.respond(503, {"error": "Sync Box unavailable"})

        def hue_bridge(self, action):
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 256:
                    return self.respond(413, {"error": "Hue bridge payload too large"})
                data = json.loads(self.rfile.read(size))
                if not isinstance(data, dict):
                    raise ValueError("Expected an object")
                if action == "pair":
                    if set(data) != {"ip"}:
                        raise ValueError("Expected an address")
                    return self.respond(200, bridge.pair(data["ip"]))
                if data:
                    raise ValueError("Expected an empty object")
                return self.respond(200, {"find": bridge.find, "cancel": bridge.cancel}[action]())
            except Configured:
                return self.respond(409, {"error": "the service configuration names a Hue bridge",
                                          "reason": "configured"})
            except OtherBridge:
                return self.respond(409, {"error": "another Hue bridge has connected lights",
                                          "reason": "other_bridge"})
            except BridgeError:
                return self.respond(503, {"error": "Hue bridge not found"})
            except (ValueError, TypeError):
                return self.respond(400, {"error": "invalid Hue bridge request"})
            except OSError:
                return self.respond(503, {"error": "Hue bridge unavailable"})

        def local_request(self):
            port = self.server.server_port
            # Loopback plus the LAN names the owner listed; any other Host
            # (DNS rebinding) or a cross-site Origin is refused.
            accepted = {f"{name}:{port}" for name in ("127.0.0.1", "localhost", *hosts)}
            if self.headers.get("Host", "").lower() not in accepted:
                return False
            origin = self.headers.get("Origin")
            origins = {"http://" + host for host in accepted}
            return (origin is None or origin.lower() in origins) and (
                self.headers.get("Sec-Fetch-Site") != "cross-site"
            )

    Handler.guard = guard
    Handler.has_hue_lights = staticmethod(has_hue_lights)
    Handler.lan_search = staticmethod(lan_search)
    return Handler


def serve(token_file, state_dir, bind="127.0.0.1", port=19881,
          adapter_url="http://127.0.0.1:19880", bridge_url="http://127.0.0.1",
          hosts=(), hue_config=None):
    """Serve until interrupted; SIGTERM handling belongs to the caller."""
    handler = handler_for(
        Path(token_file), Path(state_dir), adapter_url=adapter_url, bridge_url=bridge_url,
        hosts=hosts, hue_config=hue_config)
    server = ThreadingHTTPServer((bind, port), handler)
    stop = threading.Event()
    threading.Thread(target=handler.guard.run, args=(stop,), daemon=True).start()
    print(f"Setup studio: http://{bind}:{port} (autosaved desk setup)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=19881)
    args = parser.parse_args()
    # Development entry point; the packaged service passes explicit paths.
    token = Path(__file__).resolve().parents[1] / "bridge/state/adapter-token"
    serve(token, token.parent, port=args.port)


if __name__ == "__main__":
    main()
