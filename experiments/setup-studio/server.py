"""Loopback-only setup UI spike; exposes no physical-light write operations."""

import argparse
import json
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

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


def handler_for(token_file):
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
            path = urlsplit(self.path).path
            if path in ASSETS:
                name, kind = ASSETS[path]
                return self.respond(200, (STATIC / name).read_bytes(), kind)
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
            self.respond(405, {"error": "read-only preview"})

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=19881)
    args = parser.parse_args()
    token = Path(__file__).resolve().parents[1] / "hue-bridge/state/adapter-token"
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler_for(token))
    print(
        f"Setup studio: http://127.0.0.1:{args.port} (draft preview; no light writes)", flush=True
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
