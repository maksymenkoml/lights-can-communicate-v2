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
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hue-bridge"))
from desk_layout import LAYOUT_VERSION, ROLES, LayoutStore  # noqa: E402
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
CANONICAL_FIELDS = ("name", "model", "shape", "zones")
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
        if row.get("layout_id") is not None and row["layout_id"] not in ROLES:
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


def _unavailable(mode="unknown"):
    return {"version": 1, "connected": False, "mode": mode, "sync": {"state": mode}, "lamps": {}}


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
    rows = data.get("targets") if isinstance(data.get("targets"), list) else []
    for target in rows[:128]:
        if not isinstance(target, dict) or target.get("side") not in ("left", "right"):
            continue
        native = target.get("native") if isinstance(target.get("native"), dict) else {}
        online = native.get("identity_verified") is True and _fresh(target.get("status_age"), 0.75)
        power = native.get("onOff")
        lamps["govee-" + target["side"]] = {
            "online": online,
            "on": bool(power) if online and type(power) is int and power in (0, 1) else None,
            "brightness": native.get("brightness")
            if _number(native.get("brightness"), 0, 100)
            else None,
        }
    names = {
        "strip": "hue-monitor",
        "hue_left": "play-left",
        "hue_center": "play-center",
        "hue_right": "play-right",
    }
    role_lamps = {'left':'govee-left', 'right':'govee-right', **names}
    composition = data.get('composition')
    included = set(role_lamps.values())
    if composition is not None:
        selected_roles = (composition.get('enabled_roles')
                          if isinstance(composition, dict) else None)
        included = ({role_lamps[role] for role in selected_roles}
                    if isinstance(selected_roles, list) and len(selected_roles) <= 6
                    and all(type(role) is str and role in role_lamps for role in selected_roles)
                    else set())
    hue_play = data.get("hue_play") if isinstance(data.get("hue_play"), dict) else {}
    selected = {"strip": data.get("hue_strip"), **hue_play}
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
    for identifier in ROLES:
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
        count = 9 if identifier == "hue-monitor" else 7 if identifier.startswith("govee-") else 1
        lamp.update(colors=_sample_zones(colors, count), source=source, stale=stale,
                    included=identifier in included)
    # Verified cause only; the existing state label keeps its meaning.
    health = derive_sync_health(data)
    return {
        "version": 1,
        "connected": True,
        "mode": mode,
        "sync": {"state": sync_state, "reason": health["reason"],
                 "blockers": health["blockers"]},
        "lamps": lamps,
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


def handler_for(token_file, layout_file=None, catalog_fetch=None, pilot_fetch=None,
                source_fetch=None, adapter_url="http://127.0.0.1:19880",
                bridge_url="http://127.0.0.1"):
    store = LayoutStore(layout_file or token_file.parent / "desk-layout.json")

    def default_source_fetch():
        data = _read_local_json(bridge_url + "/probe/status", 0.25)
        receiver = data.get("entertainment") if isinstance(data, dict) else None
        return receiver.get("active") if isinstance(receiver, dict) else None

    source_cache = SourceActivityCache(source_fetch or default_source_fetch)

    def pilot():
        if (store.path.parent / "live-output-hold").exists():
            return _unavailable("paused")
        try:
            if pilot_fetch is not None:
                data = pilot_fetch()
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

    def validate_bindings(data):
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
                return self.respond(200, store.response())
            if path == "/api/discovery":
                return self.respond(200, discovery())
            if path == "/api/pilot":
                return self.respond(200, pilot())
            self.respond(404, {"error": "not found"})

        def do_POST(self):
            if urlsplit(self.path).path != "/api/layout":
                return self.respond(405, {"error": "unsupported write route"})
            if not self.local_request():
                return self.respond(403, {"error": "local origin required"})
            if self.headers.get_content_type() != "application/json":
                return self.respond(415, {"error": "JSON required"})
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 16384:
                    return self.respond(413, {"error": "layout payload too large"})
                data = json.loads(self.rfile.read(size))
                with store.lock:
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
                    validate_bindings(data)
                    result = store.save(data)
                return self.respond(200, result)
            except (ValueError, TypeError, KeyError):
                return self.respond(400, {"error": "invalid desk layout"})
            except OSError:
                return self.respond(503, {"error": "layout storage unavailable"})

        def local_request(self):
            hosts = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
            if self.headers.get("Host", "").lower() not in hosts:
                return False
            origin = self.headers.get("Origin")
            return (origin is None or origin.lower() in {"http://" + host for host in hosts}) and (
                self.headers.get("Sec-Fetch-Site") != "cross-site"
            )

    return Handler


def serve(token_file, layout_file, bind="127.0.0.1", port=19881,
          adapter_url="http://127.0.0.1:19880", bridge_url="http://127.0.0.1"):
    """Serve until interrupted; SIGTERM handling belongs to the caller."""
    server = ThreadingHTTPServer((bind, port), handler_for(
        Path(token_file), Path(layout_file), adapter_url=adapter_url, bridge_url=bridge_url))
    print(f"Setup studio: http://{bind}:{port} (autosaved desk setup)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=19881)
    args = parser.parse_args()
    # Development entry point; the packaged service passes explicit paths.
    token = Path(__file__).resolve().parents[1] / "hue-bridge/state/adapter-token"
    serve(token, token.parent / "desk-layout.json", port=args.port)


if __name__ == "__main__":
    main()
