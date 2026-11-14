"""Loopback setup UI with validated desk geometry and calibration persistence."""

import argparse
import json
import sys
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hue-bridge"))
from desk_layout import LayoutStore  # noqa: E402

STATIC = Path(__file__).resolve().parent / "static"
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}


def project_status(data):
    video = data.get("video", {})
    selected = {"strip": data.get("hue_strip", {}), **data.get("hue_play", {})}
    lamps = {}
    for target in data.get("targets", []):
        if target.get("side") not in ("left", "right"):
            continue
        native = target.get("native") or {}
        lamps["govee-" + target["side"]] = {
            "online": native.get("identity_verified") is True,
            "on": native.get("onOff") == 1,
            "brightness": native.get("brightness"),
        }
    names = {
        "strip": "hue-monitor",
        "hue_left": "play-left",
        "hue_center": "play-center",
        "hue_right": "play-right",
    }
    for role, identifier in names.items():
        state = selected.get(role, {})
        lamps[identifier] = {
            "online": state.get("healthy") is True,
            "on": state.get("native_on"),
            "brightness": state.get("native_brightness"),
        }
    return {
        "connected": True,
        "mode": "sync"
        if video.get("active") and video.get("ready")
        else "waiting"
        if video.get("active")
        else "scene",
        "lamps": lamps,
    }


def handler_for(token_file, layout_file=None):
    store = LayoutStore(layout_file or token_file.parent / "desk-layout.json")

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
                return self.respond(200, (STATIC / name).read_bytes(), kind)
            if path == "/api/layout":
                return self.respond(200, store.response())
            if path == "/api/pilot":
                try:
                    token = token_file.read_text().strip()
                    request = urllib.request.Request(
                        "http://127.0.0.1:19880/status", headers={"X-Adapter-Token": token}
                    )
                    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                    with opener.open(request, timeout=1) as response:
                        data = json.load(response)
                    return self.respond(200, project_status(data))
                except (OSError, ValueError, KeyError, TypeError):
                    return self.respond(200, {"connected": False, "mode": "unknown", "lamps": {}})
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
                    expected = self.headers.get("If-Match")
                    if expected is not None and expected.strip('"') != (store.revision() or ""):
                        return self.respond(409, {"error": "layout changed"})
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=19881)
    args = parser.parse_args()
    token = Path(__file__).resolve().parents[1] / "hue-bridge/state/adapter-token"
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler_for(token))
    print(f"Setup studio: http://127.0.0.1:{args.port} (autosaved desk setup)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
