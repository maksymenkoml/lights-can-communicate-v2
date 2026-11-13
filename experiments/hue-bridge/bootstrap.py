"""Disposable Hue app compatibility probe. All lights use the dummy protocol."""

import hmac
import json
import logging
import os
import secrets
import sys
import time
import uuid
from pathlib import Path

# The upstream pairing handler logs generated API keys at INFO. Keep them private.
logging.disable(logging.CRITICAL)
sys.path.insert(0, "/opt/hue-emulator")

import configManager  # noqa: E402
from flask import abort, jsonify, request  # noqa: E402
from HueObjects import ApiUser, Group, Light  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402

state = Path(configManager.runtimeConfig.arg["CONFIG_PATH"])
state.chmod(0o700)


def local_secret(name, size):
    path = state / name
    if not path.exists():
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(secrets.token_hex(size) + "\n")
    return path.read_text().strip()


token = local_secret("probe-token", 24)
api_key = local_secret("probe-api-key", 16)
config = configManager.bridgeConfig.yaml_config
settings = config["config"]
settings.update(
    {
        "name": "Smart Lighting Probe",
        "discovery": False,
        "Remote API enabled": False,
        "timezone": "America/Toronto",
        "apiversion": "1.78.0",
        "swversion": "1978545030",
        "users": {"probe@localhost": {"password": generate_password_hash(token)}},
    }
)
for protocol in (
    "mqtt",
    "deconz",
    "homeassistant",
    "yeelight",
    "native_multi",
    "tasmota",
    "wled",
    "shelly",
    "esphome",
    "hyperion",
    "tpkasa",
    "elgato",
    "govee",
    "hue_bl",
):
    settings[protocol] = {"enabled": False}
settings["hue"] = {}
settings["tradfri"] = {}
if any(light.protocol != "dummy" for light in config["lights"].values()):
    raise RuntimeError("This probe refuses physical-device protocols")
for index, side in enumerate(("Left", "Right"), start=1):
    identifier = str(index)
    if identifier not in config["lights"]:
        lamp = Light.Light(
            {
                "id_v1": identifier,
                "id_v2": str(uuid.uuid5(uuid.NAMESPACE_URL, f"smart-lighting-probe/{side}")),
                "name": f"Govee {side} TEST",
                "modelid": "LCT015",
                "protocol": "dummy",
            }
        )
        lamp.state.update({"on": True, "bri": 200, "reachable": True})
        config["lights"][identifier] = lamp
        config["groups"]["0"].add_light(lamp)
if "1" not in config["groups"]:
    room = Group.Group(
        {"id_v1": "1", "name": "Probe Office TEST", "type": "Room", "class": "Office"}
    )
    for lamp in config["lights"].values():
        room.add_light(lamp)
    config["groups"]["1"] = room
if api_key not in config["apiUsers"]:
    config["apiUsers"][api_key] = ApiUser.ApiUser(api_key, "SmartLightingProbe#selftest", None)
configManager.bridgeConfig.save_config(backup=False)

from HueEmulator3 import app, main  # noqa: E402
from lights.protocols import protocols  # noqa: E402
from services import updateManager  # noqa: E402


def hold_probe_capability_versions():
    """Keep the public BSB002 version fixture independent of online release scraping."""


updateManager.startupCheck = hold_probe_capability_versions


def reject_physical_output(*args, **kwargs):
    raise RuntimeError("Physical output is disabled in the compatibility probe")


for protocol in protocols:
    protocol.set_light = reject_physical_output


@app.after_request
def record_request(response):
    if request.path.startswith("/probe/"):
        return response
    parts = request.path.split("/")
    if len(parts) > 2 and parts[1] == "api" and parts[2] not in ("", "config"):
        parts[2] = "<key>"
    payload = request.get_json(silent=True)
    event = {
        "time": time.time(),
        "method": request.method,
        "path": "/".join(parts),
        "status": response.status_code,
        "fields": sorted(payload) if isinstance(payload, dict) else [],
        "user_agent": request.headers.get("User-Agent", ""),
    }
    with (state / "requests.jsonl").open("a") as stream:
        stream.write(json.dumps(event) + "\n")
    return response


@app.post("/probe/link")
def link_probe():
    if not hmac.compare_digest(request.headers.get("X-Probe-Token", ""), token):
        abort(403)
    settings["linkbutton"]["lastlinkbuttonpushed"] = time.time()
    return jsonify({"pairing_window_seconds": 30})


@app.get("/probe/status")
def probe_status():
    return jsonify(
        {
            "name": settings["name"],
            "bridgeid": settings["bridgeid"],
            "simulation_only": True,
            "clients": [
                getattr(client, "name", "unknown") for client in config["apiUsers"].values()
            ],
            "lights": [
                {
                    "id": lamp.id_v2,
                    "name": lamp.name,
                    "protocol": lamp.protocol,
                    "state": lamp.state,
                    "dynamics": lamp.dynamics,
                }
                for lamp in config["lights"].values()
            ],
            "scenes": [
                {"name": getattr(scene, "name", "unknown"), "speed": getattr(scene, "speed", None)}
                for scene in config["scenes"].values()
            ],
        }
    )


os.environ["TZ"] = "America/Toronto"
time.tzset()
main()
