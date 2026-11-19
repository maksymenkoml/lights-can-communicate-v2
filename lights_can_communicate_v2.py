#!/usr/bin/env python3
"""Original local Hue -> Govee palette CLI. Python standard library only."""

import argparse
import ipaddress
import json
import math
import os
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

CONFIG = Path(__file__).with_name(".lighting.json")
MULTICAST = "239.255.255.250"
GOVEE_ROUTES = {}
VERIFIED_GOVEE = {}
# Key -> (ip, proof time) for a pinned lamp whose identity and a fresh native
# status were just proven, whether it is on or off. Only power commands use it:
# a color command must never reach an off lamp, but an On must.
IDENTIFIED_GOVEE = {}
GOVEE_ROUTE_REVISIONS = {}
GOVEE_ROUTE_LOCK = threading.Lock()
NEXT_GOVEE_DISCOVERY = 0


def load_config():
    return json.loads(CONFIG.read_text()) if CONFIG.exists() else {}


def save_config(config):
    temporary = CONFIG.with_name("tmp_lighting_config.json")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(config, stream, indent=2)
            stream.write("\n")
        os.replace(temporary, CONFIG)
    finally:
        temporary.unlink(missing_ok=True)


def local_ip(value):
    address = ipaddress.IPv4Address(value)
    if not address.is_private or address.is_multicast or address.is_unspecified:
        raise ValueError("Expected a private local IPv4 address")
    return str(address)


def request_json(url, data=None, headers=None, context=None):
    request = urllib.request.Request(
        url,
        data=None if data is None else json.dumps(data).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    # Local device requests must not go through a configured HTTP proxy.
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context)
    )
    with opener.open(request, timeout=5) as response:
        return json.load(response)


def hue_request(config, path, data=None):
    bridge = local_ip(config["bridge"])
    # Hue Bridge uses a self-signed certificate. This exception is local to Hue.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    headers = {"hue-application-key": config["hue_key"]} if config.get("hue_key") else {}
    response = request_json(f"https://{bridge}{path}", data, headers, context)
    if isinstance(response, dict) and response.get("errors"):
        raise ValueError(f"Hue error: {response['errors']}")
    return response


def udp_packet(command, data):
    return json.dumps({"msg": {"cmd": command, "data": data}}).encode()


def govee_discover(timeout=5):
    found = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", 4002))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.setsockopt(
            socket.IPPROTO_IP,
            socket.IP_ADD_MEMBERSHIP,
            socket.inet_aton(MULTICAST) + socket.inet_aton("0.0.0.0"),
        )
        end = time.monotonic() + timeout
        next_scan = 0
        while time.monotonic() < end:
            if time.monotonic() >= next_scan:
                sock.sendto(udp_packet("scan", {"account_topic": "reserve"}), (MULTICAST, 4001))
                next_scan = time.monotonic() + 1
            sock.settimeout(min(0.5, max(0.01, end - time.monotonic())))
            try:
                raw, address = sock.recvfrom(8192)
                message = json.loads(raw).get("msg", {})
                if message.get("cmd") == "scan":
                    device = {**message["data"], "ip": address[0]}
                    invalidate_govee_observations([device])
                    found[(address[0], device_key(device))] = device
            except (socket.timeout, ValueError, KeyError, TypeError, AttributeError):
                continue
    return list(found.values())


def govee_send(ip, command, data):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.sendto(udp_packet(command, data), (local_ip(ip), 4003))


def device_key(device):
    value = device.get("device", "")
    compact = value.replace(":", "").upper()
    if len(compact) != 16 or any(char not in "0123456789ABCDEF" for char in compact):
        raise ValueError("Govee target has no valid pinned device ID; run configure first")
    return ":".join(compact[index : index + 2] for index in range(0, 16, 2))


def resolve_govee_targets(targets, discovered):
    candidates = {}
    addresses = {}
    for item in discovered:
        try:
            key, ip = device_key(item), local_ip(item["ip"])
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
        candidates.setdefault(key, set()).add(ip)
        addresses.setdefault(ip, set()).add(key)
    resolved = {}
    for target in targets:
        key = device_key(target)
        matches = candidates.get(key, set())
        if len(matches) == 1:
            ip = next(iter(matches))
            if len(addresses[ip]) == 1:
                resolved[key] = ip
    return resolved


def invalidate_govee_observations(discovered):
    with GOVEE_ROUTE_LOCK:
        for item in discovered:
            try:
                ip = local_ip(item["ip"])
            except (KeyError, TypeError, ValueError):
                continue
            try:
                observed = device_key(item)
            except (ValueError, AttributeError, TypeError):
                observed = None
            known = dict(GOVEE_ROUTES)
            known.update({key: binding[0] for key, binding in VERIFIED_GOVEE.items()})
            known.update({key: binding[0] for key, binding in IDENTIFIED_GOVEE.items()})
            for key, address in known.items():
                if (ip == address and observed != key) or (observed == key and ip != address):
                    GOVEE_ROUTE_REVISIONS[key] = GOVEE_ROUTE_REVISIONS.get(key, 0) + 1
                    VERIFIED_GOVEE.pop(key, None)
                    IDENTIFIED_GOVEE.pop(key, None)
                    GOVEE_ROUTES.pop(key, None)


def govee_scan_at(ip, timeout=0.15):
    ip = local_ip(ip)
    identities = {}
    invalid_identity = False
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", 4002))
        sock.sendto(udp_packet("scan", {"account_topic": "reserve"}), (ip, 4001))
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            sock.settimeout(max(0.01, end - time.monotonic()))
            try:
                raw, peer = sock.recvfrom(8192)
                message = json.loads(raw).get("msg", {})
                if message.get("cmd") == "scan":
                    identity = {**message["data"], "ip": peer[0], "_observed_at": time.monotonic()}
                    invalidate_govee_observations([identity])
                    if peer[0] != ip:
                        continue
                    try:
                        key = device_key(identity)
                    except (ValueError, TypeError, AttributeError):
                        invalid_identity = True
                        continue
                    identities[key] = identity
            except (socket.timeout, ValueError, KeyError, TypeError, AttributeError):
                continue
    if len(identities) == 1 and not invalid_identity:
        return next(iter(identities.values()))
    return None


def verified_status(device, timeout=0.15, targets=None):
    global NEXT_GOVEE_DISCOVERY
    key = device_key(device)
    with GOVEE_ROUTE_LOCK:
        ip = GOVEE_ROUTES.get(key, device["ip"])
    identity = govee_scan_at(ip, timeout=min(timeout, 0.15))
    verified_at = identity.get("_observed_at", time.monotonic()) if identity else time.monotonic()
    matches = identity is not None and resolve_govee_targets([device], [identity]).get(key) == ip
    if not matches:
        with GOVEE_ROUTE_LOCK:
            if identity is not None:
                VERIFIED_GOVEE.pop(key, None)
                IDENTIFIED_GOVEE.pop(key, None)
                GOVEE_ROUTES.pop(key, None)
            search = time.monotonic() >= NEXT_GOVEE_DISCOVERY
            if search:
                NEXT_GOVEE_DISCOVERY = time.monotonic() + 5
        if not search:
            return None
        discovered = govee_discover(timeout=0.6)
        invalidate_govee_observations(discovered)
        routes = resolve_govee_targets(targets or [device], discovered)
        with GOVEE_ROUTE_LOCK:
            for found_key, found_ip in routes.items():
                if GOVEE_ROUTES.get(found_key) != found_ip:
                    VERIFIED_GOVEE.pop(found_key, None)
                    IDENTIFIED_GOVEE.pop(found_key, None)
                GOVEE_ROUTES[found_key] = found_ip
        if key not in routes:
            return None
        ip = routes[key]
        identity = govee_scan_at(ip, timeout=min(timeout, 0.15))
        verified_at = (
            identity.get("_observed_at", time.monotonic()) if identity else time.monotonic()
        )
        if identity is None or resolve_govee_targets([device], [identity]).get(key) != ip:
            with GOVEE_ROUTE_LOCK:
                VERIFIED_GOVEE.pop(key, None)
                IDENTIFIED_GOVEE.pop(key, None)
            return None
    with GOVEE_ROUTE_LOCK:
        GOVEE_ROUTES[key] = ip
        revision = GOVEE_ROUTE_REVISIONS.get(key, 0)
    status = govee_status(ip, timeout=timeout, expected_device=key)
    with GOVEE_ROUTE_LOCK:
        if GOVEE_ROUTE_REVISIONS.get(key, 0) != revision or GOVEE_ROUTES.get(key) != ip:
            return None
        GOVEE_ROUTES[key] = ip
        if status is not None:
            if status.get("onOff") == 1:
                VERIFIED_GOVEE[key] = (ip, verified_at)
            else:
                VERIFIED_GOVEE.pop(key, None)
    return status


def send_to_target(device, command, data):
    key = device_key(device)
    with GOVEE_ROUTE_LOCK:
        binding = VERIFIED_GOVEE.get(key)
        if binding is None or not 0 <= time.monotonic() - binding[1] <= 0.75:
            return False
        govee_send(binding[0], command, data)
    return True


def send_power_to_target(device, command, data):
    """Send a power command to a pinned lamp proven on or off; others as colors.

    A ``turn`` goes over the freshly identified route, so an Off lamp can be
    switched on. Every other command keeps ``send_to_target``'s rule: only a lamp
    proven on receives it.
    """
    if command != "turn":
        return send_to_target(device, command, data)
    key = device_key(device)
    with GOVEE_ROUTE_LOCK:
        binding = IDENTIFIED_GOVEE.get(key) or VERIFIED_GOVEE.get(key)
        if binding is None or not 0 <= time.monotonic() - binding[1] <= 0.75:
            return False
        govee_send(binding[0], command, data)
    return True


def remember_govee_routes(config):
    changed = False
    with GOVEE_ROUTE_LOCK:
        routes = dict(GOVEE_ROUTES)
    for device in config["targets"]:
        ip = routes.get(device_key(device))
        if ip is not None and ip != device["ip"]:
            print(f"{device['side']} Govee address changed: {device['ip']} -> {ip}", flush=True)
            device["ip"] = ip
            changed = True
    if changed:
        save_config(config)


def govee_status(ip, timeout=3, expected_device=None):
    ip = local_ip(ip)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", 4002))
        sock.sendto(udp_packet("devStatus", {}), (ip, 4003))
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            sock.settimeout(max(0.01, end - time.monotonic()))
            try:
                raw, address = sock.recvfrom(8192)
                message = json.loads(raw).get("msg", {})
                if message.get("cmd") == "scan":
                    identity = {**message["data"], "ip": address[0]}
                    invalidate_govee_observations([identity])
                    if address[0] == ip and expected_device is not None:
                        try:
                            matches = device_key(identity) == expected_device
                        except (ValueError, TypeError, AttributeError):
                            matches = False
                        if not matches:
                            return None
                    continue
                if address[0] == ip and message.get("cmd") == "devStatus":
                    return message["data"]
            except (socket.timeout, ValueError, KeyError, TypeError, AttributeError):
                continue
    return None


def xy_to_rgb(x, y):
    if not (math.isfinite(x) and math.isfinite(y) and 0 <= x <= 1 and 0 < y <= 1):
        raise ValueError("Invalid Hue xy color")
    if x + y > 1.0001:
        raise ValueError("Hue xy color is outside the chromaticity diagram")
    xx, yy, zz = x / y, 1, (1 - x - y) / y
    linear = [
        max(0, 3.2406 * xx - 1.5372 * yy - 0.4986 * zz),
        max(0, -0.9689 * xx + 1.8758 * yy + 0.0415 * zz),
        max(0, 0.0557 * xx - 0.2040 * yy + 1.0570 * zz),
    ]
    peak = max(1, *linear)
    linear = [value / peak for value in linear]
    return tuple(
        round(255 * (12.92 * value if value <= 0.0031308 else 1.055 * value ** (1 / 2.4) - 0.055))
        for value in linear
    )


def animation_settings(light):
    dynamics = light.get("dynamics", {})
    points = light.get("gradient", {}).get("points", [])
    if (
        not light["on"]["on"]
        or light.get("mode") == "streaming"
        or dynamics.get("status") != "dynamic_palette"
        or light.get("color_temperature", {}).get("mirek_valid")
        or len(points) < 2
    ):
        return None
    speed = dynamics.get("speed", 0.5) if dynamics.get("speed_valid") else 0.5
    speed = max(0, min(1, speed))
    colors = tuple((point["color"]["xy"]["x"], point["color"]["xy"]["y"]) for point in points)
    # Approximate pacing, not Hue's private animation timing or phase.
    return colors, 12 - 10 * speed


def parse_brightness_scale(value):
    scale = float(value)
    if not math.isfinite(scale) or not 0 < scale <= 1:
        raise ValueError("Brightness scale must be greater than 0 and no more than 1")
    return scale


def target_for(light, side, elapsed=0, brightness_scale=1):
    if not light["on"]["on"]:
        return {"on": False}
    scale = parse_brightness_scale(brightness_scale)
    brightness = round(light.get("dimming", {}).get("brightness", 100) * scale)
    target = {"on": True, "brightness": max(1, min(100, brightness))}
    temperature = light.get("color_temperature", {})
    if temperature.get("mirek_valid") and temperature.get("mirek"):
        target["kelvin"] = max(2000, min(9000, round(1_000_000 / temperature["mirek"])))
    else:
        points = light.get("gradient", {}).get("points", [])
        if points:
            index = {"left": 0, "right": len(points) - 1, "center": len(points) // 2}[side]
            color = points[index]["color"]["xy"]
            settings = animation_settings(light)
            if settings:
                colors, step_seconds = settings
                position = (index + max(0, elapsed) / step_seconds) % len(colors)
                start = int(position)
                amount = position - start
                amount = amount * amount * (3 - 2 * amount)
                first, second = colors[start], colors[(start + 1) % len(colors)]
                color = {
                    "x": first[0] + (second[0] - first[0]) * amount,
                    "y": first[1] + (second[1] - first[1]) * amount,
                }
        else:
            color = light["color"]["xy"]
        target["rgb"] = xy_to_rgb(color["x"], color["y"])
    return target


def govee_commands(target):
    if not target["on"]:
        return []
    rgb = target.get("rgb", (0, 0, 0))
    return [
        (
            "colorwc",
            {"color": dict(zip(("r", "g", "b"), rgb)), "colorTemInKelvin": target.get("kelvin", 0)},
        ),
        ("brightness", {"value": target["brightness"]}),
    ]


def animate_frame(light, devices, elapsed, previous, statuses=None):
    for device in devices:
        key = device_key(device)
        status = (
            verified_status(device, timeout=0.2, targets=devices)
            if statuses is None
            else statuses.get(key)
        )
        if status is None or status.get("onOff") != 1:
            previous.pop(key, None)
            continue
        target = target_for(light, device["side"], elapsed, device.get("brightness_scale", 1))
        if target["on"] and target != previous.get(key):
            sent = False
            for command, data in govee_commands(target):
                if command == "colorwc":
                    sent = send_to_target(device, command, data)
            if sent:
                previous[key] = target


def read_govee_states(devices):
    # A single worker owns UDP receive port 4002; parallel readers steal replies.
    states = {}
    for device in devices:
        status = verified_status(device, timeout=0.15, targets=devices)
        states[device_key(device)] = (status, time.monotonic())
    return states


def run_dynamic(config, light, previous, interval, started, initial_statuses):
    now = time.monotonic()
    source_seen = now
    palette_key = animation_settings(light)
    states = {ip: (status, now) for ip, status in initial_statuses.items()}
    next_source, next_status, deadline = now + interval, now, now
    source_future = status_future = None
    brightness_refresh = now + 30
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="lighting-io") as reader:
        while True:
            now = time.monotonic()
            if source_future is not None and source_future.done():
                try:
                    light = source_future.result()["data"][0]
                    source_seen = now
                except OSError as error:
                    source_seen = float("-inf")
                    print(f"Hue read failed; animation paused: {error}", file=sys.stderr)
                source_future = None
                next_source = now + interval
                settings = animation_settings(light)
                if not settings:
                    return light
                if settings != palette_key:
                    palette_key, started = settings, now
                    print("Dynamic palette changed; starting its local cycle.", flush=True)
            if status_future is not None and status_future.done():
                try:
                    states.update(
                        {
                            ip: value
                            for ip, value in status_future.result().items()
                            if value[0] is not None
                        }
                    )
                except OSError:
                    states.clear()
                remember_govee_routes(config)
                status_future = None
                next_status = now + 0.2
            if source_future is None and now >= next_source:
                source_future = reader.submit(
                    hue_request, config, f"/clip/v2/resource/light/{config['light']}"
                )
            if status_future is None and now >= next_status:
                status_future = reader.submit(read_govee_states, config["targets"])
            fresh = {
                ip: status if now - seen <= 0.75 else None for ip, (status, seen) in states.items()
            }
            if now - source_seen <= interval + 1:
                for device in config["targets"]:
                    key = device_key(device)
                    status = fresh.get(key)
                    if status is None or status.get("onOff") != 1:
                        continue
                    target = target_for(
                        light, device["side"], now - started, device.get("brightness_scale", 1)
                    )
                    old = previous.get(key, {})
                    if old.get("brightness") != target["brightness"] or now >= brightness_refresh:
                        send_to_target(device, "brightness", {"value": target["brightness"]})
                animate_frame(light, config["targets"], now - started, previous, statuses=fresh)
                if now >= brightness_refresh:
                    brightness_refresh = now + 30
            # Fixed deadlines avoid adding network/work duration to each frame.
            deadline += 1 / 20
            delay = deadline - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                deadline = time.monotonic()


def run_sync(config, once=False, dry_run=False, interval=2):
    if not config.get("light") or not config.get("targets"):
        raise ValueError("Run lights and configure first; no Hue source or Govee targets selected")
    keys = [device_key(device) for device in config["targets"]]
    if len(set(keys)) != len(keys):
        raise ValueError("Each selected Govee must have a distinct pinned device ID")
    previous = {}
    last_refresh = 0
    paused = False
    unavailable = set()
    animation_key = None
    animation_started = 0
    print("Hue -> Govee. Ctrl+C stops following; lights keep their last state.", flush=True)
    while True:
        light = hue_request(config, f"/clip/v2/resource/light/{config['light']}")["data"][0]
        if light.get("mode") == "streaming":
            if not paused:
                print("Paused: Hue Sync is active. Govee keep their last colors.", flush=True)
            paused = True
            animation_key = None
            previous.clear()
            if once:
                return
            time.sleep(interval)
            continue
        if paused:
            print("Hue Sync ended. Following the stored palette again.", flush=True)
            paused = False
        settings = animation_settings(light)
        if settings != animation_key:
            animation_started = time.monotonic()
            animation_key = settings
            if settings:
                print(
                    "Dynamic palette: animating Govee locally; Hue phase is unavailable.",
                    flush=True,
                )
            else:
                print("Static palette: stopped local animation.", flush=True)
        refresh = time.monotonic() - last_refresh >= 30
        changed = []
        active_devices = []
        for device in config["targets"]:
            key = device_key(device)
            target = target_for(
                light,
                device["side"],
                time.monotonic() - animation_started,
                device.get("brightness_scale", 1),
            )
            if not target["on"]:
                previous.pop(key, None)
                continue
            if not dry_run:
                status = verified_status(device, targets=config["targets"])
                remember_govee_routes(config)
                if status is None:
                    if once:
                        raise ValueError(f"Govee {device['ip']} did not reply; no color sent to it")
                    if key not in unavailable:
                        print(
                            f"Govee {device['ip']} did not reply; retrying next cycle.",
                            file=sys.stderr,
                            flush=True,
                        )
                    unavailable.add(key)
                    previous.pop(key, None)
                    continue
                if key in unavailable:
                    print(f"Govee {device['ip']} is responding again.", flush=True)
                    unavailable.remove(key)
                if status.get("onOff") != 1:
                    previous.pop(key, None)
                    continue
            active_devices.append(device)
            if target != previous.get(key) or refresh:
                print(f"{device['side']} {device['ip']}: {json.dumps(target)}", flush=True)
                if not dry_run:
                    sent_all = True
                    for command, data in govee_commands(target):
                        sent_all = send_to_target(device, command, data) and sent_all
                        time.sleep(0.08)
                    if sent_all:
                        changed.append(device)
                    else:
                        previous.pop(key, None)
                        continue
                previous[key] = target
        if refresh:
            last_refresh = time.monotonic()
        if once:
            if not dry_run:
                time.sleep(0.3)
                for device in changed:
                    status = verified_status(device, targets=config["targets"])
                    print(f"Readback {device['ip']}: {json.dumps(status)}", flush=True)
                    if status is None:
                        raise ValueError(
                            "Govee did not confirm its state; physical result is unverified"
                        )
            return
        if settings and active_devices and not dry_run:
            run_dynamic(
                config,
                light,
                previous,
                interval,
                animation_started,
                initial_statuses={device_key(device): {"onOff": 1} for device in active_devices},
            )
        else:
            time.sleep(interval)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("discover", help="Find Hue Bridge and LAN-enabled Govee; save their addresses")
    pair = sub.add_parser("pair", help="Press the physical Hue Bridge button while this runs")
    pair.add_argument("--bridge", type=local_ip)
    sub.add_parser("lights", help="List Hue lights and their current palette")
    configure = sub.add_parser(
        "configure", help="Select the Hue source and one or two Govee controllers"
    )
    configure.add_argument("--light", required=True)
    configure.add_argument("--left", type=local_ip)
    configure.add_argument("--right", type=local_ip)
    configure.add_argument(
        "--govee", type=local_ip, help="Single controller: use the center Hue color"
    )
    sync = sub.add_parser("sync", help="Follow the Hue palette until Ctrl+C")
    sync.add_argument("--once", action="store_true")
    sync.add_argument(
        "--dry-run", action="store_true", help="Read Hue and print targets without changing Govee"
    )
    sync.add_argument("--interval", type=float, default=2)
    calibrate = sub.add_parser("calibrate", help="Save a brightness multiplier for each Govee")
    calibrate.add_argument("--left", type=parse_brightness_scale)
    calibrate.add_argument("--right", type=parse_brightness_scale)
    calibrate.add_argument(
        "--govee", type=parse_brightness_scale, help="Single controller multiplier"
    )
    sub.add_parser("status", help="Read selected Govee controllers without changing them")
    args = parser.parse_args()
    config = load_config()
    if args.command == "discover":
        try:
            bridges = request_json("https://discovery.meethue.com/")
            print("Hue:", json.dumps(bridges))
            if len(bridges) == 1:
                bridge = local_ip(bridges[0]["internalipaddress"])
                if config.get("bridge") and config["bridge"] != bridge:
                    print("Saved bridge differs; use pair --bridge to select the replacement")
                else:
                    config["bridge"] = bridge
        except (OSError, ValueError) as error:
            print(f"Hue discovery unavailable: {error}. You can use pair --bridge IP.")
        devices = govee_discover()
        config["discovered_govee"] = devices
        save_config(config)
        print("Govee:", json.dumps(devices, indent=2))
        if not devices:
            print("No Govee replies. Enable LAN Control in Govee Home and check the Wi-Fi network.")
    elif args.command == "pair":
        if args.bridge:
            if args.bridge != config.get("bridge"):
                config.pop("hue_key", None)
                config.pop("light", None)
            config["bridge"] = args.bridge
        if not config.get("bridge"):
            raise ValueError("Run discover or supply pair --bridge IP")
        if config.get("hue_key"):
            hue_request(config, "/clip/v2/resource/light")
            print("Existing Hue pairing works.")
            return
        print("Press the physical button on Hue Bridge now. Waiting for pairing...", flush=True)
        end = time.monotonic() + 90
        while time.monotonic() < end:
            result = hue_request(
                config, "/api", {"devicetype": "lights-can-communicate-v2#desktop"})
            if result and "success" in result[0]:
                config["hue_key"] = result[0]["success"]["username"]
                save_config(config)
                print("Hue paired. Key saved locally with owner-only permissions.", flush=True)
                return
            error = result[0].get("error", {})
            if error.get("type") != 101:
                raise ValueError(f"Hue pairing failed: {error}")
            time.sleep(2)
        raise ValueError("Pairing window expired; run pair and press the Hue Bridge button")
    elif args.command == "lights":
        for light in hue_request(config, "/clip/v2/resource/light")["data"]:
            print(
                json.dumps(
                    {
                        key: light.get(key)
                        for key in (
                            "id",
                            "metadata",
                            "on",
                            "mode",
                            "dimming",
                            "color",
                            "color_temperature",
                            "gradient",
                        )
                    },
                    indent=2,
                )
            )
    elif args.command == "calibrate":
        scales = {"left": args.left, "right": args.right, "center": args.govee}
        updates = {side: scale for side, scale in scales.items() if scale is not None}
        if not updates:
            parser.error("Supply --left SCALE, --right SCALE, or --govee SCALE (e.g. 0.5)")
        sides = {device["side"] for device in config.get("targets", [])}
        if updates.keys() - sides:
            parser.error("Calibration side is not configured; run configure first")
        for device in config["targets"]:
            if device["side"] in updates:
                device["brightness_scale"] = updates[device["side"]]
        save_config(config)
        print("Brightness calibration saved. Restart sync to apply it.")
    elif args.command == "configure":
        if args.govee and (args.left or args.right):
            parser.error("Use --govee for one controller, or --left and --right for two")
        if not args.govee and not (args.left and args.right):
            parser.error("Supply --govee IP or both --left IP --right IP")
        if args.left and args.left == args.right:
            parser.error(
                "A paired set on one IP needs --govee IP; LAN cannot address its bars separately"
            )
        lights = hue_request(config, "/clip/v2/resource/light")["data"]
        if not any(light["id"] == args.light for light in lights):
            raise ValueError("Selected Hue light was not found on this bridge")
        targets = (
            [{"ip": args.govee, "side": "center"}]
            if args.govee
            else [{"ip": args.left, "side": "left"}, {"ip": args.right, "side": "right"}]
        )
        found = govee_discover()
        old_scales = {
            item["side"]: item.get("brightness_scale", 1) for item in config.get("targets", [])
        }
        for device in targets:
            matches = [item for item in found if item.get("ip") == device["ip"]]
            if len(matches) != 1:
                raise ValueError("Selected Govee address did not return one unambiguous device ID")
            device["device"] = device_key(matches[0])
            if resolve_govee_targets([device], found).get(device["device"]) != device["ip"]:
                raise ValueError("Selected Govee identity is ambiguous; no configuration saved")
            device["brightness_scale"] = old_scales.get(device["side"], 1)
        if len({device["device"] for device in targets}) != len(targets):
            raise ValueError("Left and right must be different Govee device IDs")
        config["light"], config["targets"] = args.light, targets
        save_config(config)
        print("Saved selection. Try sync --once --dry-run before sync --once.")
    elif args.command == "status":
        for device in config.get("targets", []):
            status = verified_status(device, targets=config["targets"])
            remember_govee_routes(config)
            print(device["side"], device["ip"], device_key(device), json.dumps(status))
    elif args.command == "sync":
        if not math.isfinite(args.interval) or args.interval < 1:
            parser.error("--interval must be at least 1 second")
        run_sync(config, args.once, args.dry_run, args.interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped. Lights keep their last state.")
    except (OSError, ValueError, KeyError, IndexError) as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)
