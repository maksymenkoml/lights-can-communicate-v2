"""Hue compatibility probe with an optional allowlisted host-side live adapter."""

import hmac
import json
import logging
import os
import secrets
import sys
import threading
import time
import uuid
from pathlib import Path

# The upstream pairing handler logs generated API keys at INFO. Keep them private.
logging.disable(logging.CRITICAL)
sys.path.insert(0, "/opt/hue-emulator")

from hue_events import install_event_journal, install_event_route  # noqa: E402

# Capture publication before configManager loads Hue object modules and their
# imported StreamEvent aliases. The original legacy queue remains intact.
event_journal = install_event_journal()

import configManager  # noqa: E402
from desk_layout import LayoutStore, layout_event  # noqa: E402
from desk_layout import positions as layout_positions  # noqa: E402
from entertainment_stream import GRADIENT_MODE, GROUP_ID, POSITIONS, SLOT_CHANNELS  # noqa: E402
from flask import abort, jsonify, request  # noqa: E402
from gradient_profile import configure_signe, validate_gradient  # noqa: E402
from hue_resource_compat import canonical_collection, install_resource_compat  # noqa: E402
from HueObjects import (  # noqa: E402
    ApiUser,
    EntertainmentConfiguration,
    Group,
    Light,
    Scene,
    StreamEvent,
)
from werkzeug.security import generate_password_hash  # noqa: E402

state = Path(configManager.runtimeConfig.arg["CONFIG_PATH"])
state.chmod(0o700)
layout_store = LayoutStore(state / "desk-layout.json")


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
include_strip = os.environ.get("PROBE_HUE_STRIP", "false").lower() == "true"
include_hue_play = os.environ.get("PROBE_HUE_PLAY", "false").lower() == "true"
include_entertainment = os.environ.get("PROBE_ENTERTAINMENT", "false").lower() == "true"
selected_slots = (
    {"1", "2"}
    | ({"3"} if include_strip else set())
    | ({"4", "5", "6"} if include_hue_play else set())
)
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
    configure_signe(config["lights"][identifier], GRADIENT_MODE)
if "1" not in config["groups"]:
    room = Group.Group(
        {"id_v1": "1", "name": "Probe Office TEST", "type": "Room", "class": "Office"}
    )
    for lamp in config["lights"].values():
        room.add_light(lamp)
    config["groups"]["1"] = room
if include_strip:
    if "3" not in config["lights"]:
        lamp = Light.Light(
            {
                "id_v1": "3",
                "id_v2": str(
                    uuid.uuid5(uuid.NAMESPACE_URL, "smart-lighting-probe/HueMonitorStrip")
                ),
                "name": "Hue Monitor PROXY",
                "modelid": "LCX006",
                "protocol": "dummy",
                "protocol_cfg": {"points_capable": 5, "proxy_color_kind": "gradient"},
            }
        )
        neutral = {"x": 0.3127, "y": 0.3290}
        lamp.state.update(
            {
                "on": True,
                "bri": 200,
                "colormode": "xy",
                "xy": [neutral["x"], neutral["y"]],
                "gradient": {"points": [{"color": {"xy": dict(neutral)}} for _ in range(5)]},
            }
        )
        config["lights"]["3"] = lamp
        config["groups"]["0"].add_light(lamp)
    room = config["groups"]["1"]
    if not any(reference() is config["lights"]["3"] for reference in room.lights):
        room.add_light(config["lights"]["3"])
if include_hue_play:
    for identifier, side in (("4", "Left"), ("5", "Center"), ("6", "Right")):
        if identifier not in config["lights"]:
            lamp = Light.Light(
                {
                    "id_v1": identifier,
                    "id_v2": str(
                        uuid.uuid5(uuid.NAMESPACE_URL, "smart-lighting-probe/HuePlay" + side)
                    ),
                    "name": "Hue Play " + side + " PROXY",
                    "modelid": "LCT015",
                    "protocol": "dummy",
                    "protocol_cfg": {"proxy_color_kind": "xy"},
                }
            )
            lamp.state.update({"on": True, "bri": 200, "reachable": True})
            config["lights"][identifier] = lamp
            config["groups"]["0"].add_light(lamp)
        room = config["groups"]["1"]
        if not any(reference() is config["lights"][identifier] for reference in room.lights):
            room.add_light(config["lights"][identifier])
if include_entertainment:
    if not include_strip or not include_hue_play:
        raise RuntimeError("Desk Entertainment requires all six virtual lights")
    identifier = "90"
    if identifier not in config["groups"]:
        config["groups"][identifier] = EntertainmentConfiguration.EntertainmentConfiguration(
            {
                "id_v1": identifier,
                "id_v2": GROUP_ID,
                "name": "Desk Sync PILOT",
                "configuration_type": "monitor",
            }
        )
    area = config["groups"][identifier]
    if area.id_v2 != GROUP_ID:
        raise RuntimeError("Desk Entertainment group ID is occupied")
    area.lights = []
    for slot, channels in SLOT_CHANNELS.items():
        lamp = config["lights"][slot]
        area.add_light(lamp)
        area.locations[lamp] = [
            dict(zip(("x", "y", "z"), POSITIONS[channel])) for channel in channels
        ]

    original_area_api = EntertainmentConfiguration.EntertainmentConfiguration.getV2Api

    def desk_area_api(area):
        result = original_area_api(area)
        if area.id_v2 == GROUP_ID:
            layout = (
                layout_store.snapshot() if not (state / "layout-geometry-hold").exists() else None
            )
            coordinates = layout_positions(layout, GRADIENT_MODE) if layout else POSITIONS
            result["channels"] = []
            locations = []
            for slot, channels in SLOT_CHANNELS.items():
                lamp = config["lights"][slot]
                service = {
                    "rtype": "entertainment",
                    "rid": str(uuid.uuid5(uuid.NAMESPACE_URL, lamp.id_v2 + "entertainment")),
                }
                points = [dict(zip(("x", "y", "z"), coordinates[channel])) for channel in channels]
                locations.append(
                    {
                        "service": dict(service),
                        "positions": points,
                        "position": points[0],
                        "equalization_factor": 1,
                    }
                )
                for index, channel in enumerate(channels):
                    result["channels"].append(
                        {
                            "channel_id": channel,
                            "members": [{"index": index, "service": dict(service)}],
                            "position": dict(zip(("x", "y", "z"), coordinates[channel])),
                        }
                    )
            result["channels"].sort(key=lambda channel: channel["channel_id"])
            result["locations"] = {"service_locations": locations}
        return result

    EntertainmentConfiguration.EntertainmentConfiguration.getV2Api = desk_area_api
    original_light_entertainment = Light.Light.getV2Entertainment

    def desk_light_entertainment(lamp):
        result = original_light_entertainment(lamp)
        if lamp.id_v1 == "3":
            result["segments"] = {
                "configurable": False,
                "max_segments": 10,
                "segments": [{"start": i, "length": 1} for i in range(9)],
            }
        return result

    Light.Light.getV2Entertainment = desk_light_entertainment

    def watch_layout():
        previous = list(POSITIONS)
        while True:
            try:
                if (state / "layout-geometry-hold").exists():
                    time.sleep(0.25)
                    continue
                layout = layout_store.snapshot()
                coordinates = layout_positions(layout, GRADIENT_MODE) if layout else list(POSITIONS)
                if coordinates != previous:
                    for slot, channels in SLOT_CHANNELS.items():
                        area.locations[config["lights"][slot]] = [
                            dict(zip(("x", "y", "z"), coordinates[channel])) for channel in channels
                        ]
                    payload = desk_area_api(area)
                    StreamEvent(layout_event(payload))
                    previous = coordinates
            except (OSError, ValueError, KeyError, TypeError):
                pass
            time.sleep(0.25)

    threading.Thread(target=watch_layout, daemon=True, name="desk-layout").start()

if api_key not in config["apiUsers"]:
    config["apiUsers"][api_key] = ApiUser.ApiUser(api_key, "SmartLightingProbe#selftest", None)
configManager.bridgeConfig.save_config(backup=False)

from bridge_output import BridgeOutput, PlayGuard, desk_palette_index  # noqa: E402
from entertainment_receiver import EntertainmentReceiver  # noqa: E402
from flaskUI import restful, v2restapi  # noqa: E402
from HueEmulator3 import app, main  # noqa: E402
from lights.protocols import protocols  # noqa: E402
from power_feedback import PowerFeedback  # noqa: E402
from services import updateManager  # noqa: E402

install_resource_compat(
    v2restapi, Group.Group, settings["bridgeid"], config["groups"], Group, Light.Light
)

entertainment_receiver = EntertainmentReceiver(
    os.environ.get("PROBE_ADAPTER_URL", "http://host.docker.internal:19880"),
    local_secret("adapter-token", 24),
    enabled=include_entertainment and not (state / "live-output-hold").exists(),
)
if include_entertainment:
    entertainment_receiver.bind(area)
restful.entertainmentService = entertainment_receiver.serve
v2restapi.entertainmentService = entertainment_receiver.serve


def hold_probe_capability_versions():
    """Keep the public BSB002 version fixture independent of online release scraping."""


updateManager.startupCheck = hold_probe_capability_versions


def reject_physical_output(*args, **kwargs):
    raise RuntimeError("Physical output is disabled in the compatibility probe")


for protocol in protocols:
    protocol.set_light = reject_physical_output

live_output = None
if (
    os.environ.get("PROBE_LIVE_OUTPUT", "false").lower() == "true"
    and not (state / "live-output-hold").exists()
):
    live_output = BridgeOutput(
        os.environ["PROBE_ADAPTER_URL"],
        local_secret("adapter-token", 24),
        include_strip=include_strip,
        include_hue_play=include_hue_play,
    )

original_set_state = Light.Light._setV1State
original_dynamic_play = Light.Light.dynamicScenePlay
play_guard = PlayGuard()
power_feedback = None


Scene.Thread = play_guard.thread_factory(
    Scene.Thread,
    original_dynamic_play,
    index_selector=desk_palette_index if include_strip else None,
)
original_activate = Scene.Scene.activate


def serialized_activate(scene, values):
    with play_guard.lock:
        if values.get("recall", {}).get("action") == "deactivate":
            for reference in scene.lights:
                lamp = reference()
                if lamp is not None:
                    play_guard.cancel(lamp)
                    if live_output is not None and lamp.id_v1 in selected_slots:
                        live_output.submit(lamp.id_v1, {"on": False}, 0)
        return original_activate(scene, values)


Scene.Scene.activate = serialized_activate


def forward_selected_state(lamp, values, advertise=True):
    if GRADIENT_MODE and lamp.id_v1 in {"1", "2"} and "gradient" in values:
        try:
            validate_gradient(values["gradient"])
        except ValueError:
            abort(400)
    transitiontime = values.get("transitiontime", 4)
    changed = bool(set(values) & {"on", "bri", "xy", "ct", "hue", "sat", "gradient"})
    with play_guard.lock:
        generation = play_guard.worker_generation(lamp)
        if generation is not None and not play_guard.current(lamp, generation):
            return None
        if changed and generation is None:
            play_guard.cancel(lamp)
        if power_feedback is not None:
            values = power_feedback.constrain(lamp, values)
        result = original_set_state(lamp, values, advertise)
        if changed and lamp.id_v1 in selected_slots and live_output is not None:
            selected = {
                key: lamp.state[key]
                for key in ("on", "bri", "colormode", "xy", "ct", "hue", "sat", "gradient")
                if key in lamp.state
            }
            if lamp.id_v1 in {"3", "4", "5", "6"} or (GRADIENT_MODE and lamp.id_v1 in {"1", "2"}):
                selected["_changed"] = sorted(values)
                for field, kind in (
                    ("gradient", "gradient"),
                    ("xy", "xy"),
                    ("ct", "ct"),
                    ("hue", "hs"),
                    ("sat", "hs"),
                ):
                    if field in values:
                        lamp.protocol_cfg["proxy_color_kind"] = kind
                        break
                selected["_color_kind"] = lamp.protocol_cfg.get(
                    "proxy_color_kind", "ct" if lamp.state.get("colormode") == "ct" else "xy"
                )
            live_output.submit(lamp.id_v1, selected, transitiontime)
        return result


Light.Light._setV1State = forward_selected_state


def resume_power_color(lamp):
    selected = {
        key: lamp.state[key]
        for key in ("on", "bri", "colormode", "xy", "ct", "hue", "sat", "gradient")
        if key in lamp.state
    }
    selected["_changed"] = ["on"]
    selected["_color_kind"] = lamp.protocol_cfg.get(
        "proxy_color_kind", "ct" if lamp.state.get("colormode") == "ct" else "xy"
    )
    live_output.submit(lamp.id_v1, selected, 0)


def publish_power_groups(changed_slots):
    for group in config["groups"].values():
        if group.id_v1 != "0" and group.type not in {"Room", "Zone"}:
            continue
        if not any(ref() is not None and ref().id_v1 in changed_slots for ref in group.lights):
            continue
        resource = group.getV2GroupedLight()
        StreamEvent({
            "creationtime": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "id": str(uuid.uuid4()), "type": "update",
            "data": [{key: resource[key] for key in
                      ("id", "id_v1", "type", "owner", "on", "dimming")}],
        })


original_light_event = Light.Light.genStreamEvent
original_light_api = Light.Light.getV2Api


def advertise_selected_power(lamp, values):
    with play_guard.lock:
        if power_feedback is not None:
            values = power_feedback.constrain_v2(lamp, values)
        return original_light_event(lamp, values)


def power_aware_light_api(lamp):
    with play_guard.lock:
        result = original_light_api(lamp)
        return power_feedback.constrain_v2(lamp, result) if power_feedback is not None else result


Light.Light.genStreamEvent = advertise_selected_power
Light.Light.getV2Api = power_aware_light_api
if live_output is not None:
    power_feedback = PowerFeedback(
        live_output.url, live_output.token, config["lights"], play_guard.lock,
        resume=resume_power_color, publish_groups=publish_power_groups,
    )
    power_feedback.refresh_once()
    power_feedback.start(enabled=lambda: not (state / "live-output-hold").exists())
    live_output.start()


def current_event_snapshot():
    with play_guard.lock:
        # Capture before reading: layout metadata has an independent writer.
        # Its concurrent updates are replayed after this snapshot, never skipped.
        cursor = event_journal.position()
        created = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        resources = [lamp.getV2Api() for lamp in config["lights"].values()]
        for group in config["groups"].values():
            if group.id_v1 == "0" or group.type in {"Room", "Zone"}:
                resources.append(group.getV2GroupedLight())
            elif group.type == "Entertainment":
                resources.append(group.getV2Api())
        resources.extend(scene.getV2Api() for scene in config["scenes"].values())
        return cursor, {
            "creationtime": created,
            "id": str(uuid.uuid4()), "type": "update", "data": resources,
        }


install_event_route(app, event_journal, current_event_snapshot,
                    authorize=lambda headers: "user" in v2restapi.authorizeV2(headers))


@app.after_request
def record_request(response):
    if request.method == "GET" and request.path.rstrip("/") == "/clip/v2/resource":
        payload = response.get_json(silent=True)
        if response.status_code == 200 and isinstance(payload, dict):
            response.set_data(json.dumps(canonical_collection(payload)))
    if request.path.startswith("/probe/"):
        return response
    parts = request.path.split("/")
    if len(parts) > 2 and parts[1] == "api" and parts[2] not in ("", "config"):
        parts[2] = "<key>"
    payload = request.get_json(silent=True)
    if power_feedback is not None:
        power_feedback.after_request(request.method, request.path, response.status_code, payload)
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
            "simulation_only": live_output is None,
            "live_output_hold": (state / "live-output-hold").exists(),
            "live_output": live_output.snapshot() if live_output else {"enabled": False},
            "power_feedback": power_feedback.snapshot() if power_feedback else {"enabled": False},
            "entertainment": entertainment_receiver.snapshot(),
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
