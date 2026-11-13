#!/usr/bin/env python3
"""Small local Hue -> Govee palette experiment. Python standard library only."""

import argparse
import ipaddress
import json
import math
import os
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

CONFIG = Path(__file__).with_name(".lighting.json")
MULTICAST = "239.255.255.250"


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
                    device = message["data"]
                    found[address[0]] = {**device, "ip": address[0]}
            except (socket.timeout, json.JSONDecodeError, KeyError, TypeError):
                continue
    return list(found.values())


def govee_send(ip, command, data):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.sendto(udp_packet(command, data), (local_ip(ip), 4003))


def govee_status(ip):
    ip = local_ip(ip)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", 4002))
        sock.sendto(udp_packet("devStatus", {}), (ip, 4003))
        end = time.monotonic() + 3
        while time.monotonic() < end:
            sock.settimeout(max(0.01, end - time.monotonic()))
            try:
                raw, address = sock.recvfrom(8192)
                message = json.loads(raw).get("msg", {})
                if address[0] == ip and message.get("cmd") == "devStatus":
                    return message["data"]
            except (socket.timeout, json.JSONDecodeError, KeyError, TypeError):
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


def target_for(light, side):
    if not light["on"]["on"]:
        return {"on": False}
    brightness = round(light.get("dimming", {}).get("brightness", 100))
    target = {"on": True, "brightness": max(1, min(100, brightness))}
    temperature = light.get("color_temperature", {})
    if temperature.get("mirek_valid") and temperature.get("mirek"):
        target["kelvin"] = max(2000, min(9000, round(1_000_000 / temperature["mirek"])))
    else:
        points = light.get("gradient", {}).get("points", [])
        if points:
            index = {"left": 0, "right": len(points) - 1, "center": len(points) // 2}[side]
            color = points[index]["color"]["xy"]
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


def run_sync(config, once=False, dry_run=False, interval=2):
    if not config.get("light") or not config.get("targets"):
        raise ValueError("Run lights and configure first; no Hue source or Govee targets selected")
    previous = {}
    last_refresh = 0
    paused = False
    unavailable = set()
    print("Hue -> Govee. Ctrl+C stops following; lights keep their last state.", flush=True)
    while True:
        light = hue_request(config, f"/clip/v2/resource/light/{config['light']}")["data"][0]
        if light.get("mode") == "streaming":
            if not paused:
                print("Paused: Hue Sync is active. Govee keep their last colors.", flush=True)
            paused = True
            previous.clear()
            if once:
                return
            time.sleep(interval)
            continue
        if paused:
            print("Hue Sync ended. Following the stored palette again.", flush=True)
            paused = False
        refresh = time.monotonic() - last_refresh >= 30
        changed = []
        for device in config["targets"]:
            target = target_for(light, device["side"])
            if not target["on"]:
                previous.pop(device["ip"], None)
                continue
            if not dry_run:
                status = govee_status(device["ip"])
                if status is None:
                    if once:
                        raise ValueError(f"Govee {device['ip']} did not reply; no color sent to it")
                    if device["ip"] not in unavailable:
                        print(
                            f"Govee {device['ip']} did not reply; retrying next cycle.",
                            file=sys.stderr,
                            flush=True,
                        )
                    unavailable.add(device["ip"])
                    previous.pop(device["ip"], None)
                    continue
                if device["ip"] in unavailable:
                    print(f"Govee {device['ip']} is responding again.", flush=True)
                    unavailable.remove(device["ip"])
                if status.get("onOff") != 1:
                    previous.pop(device["ip"], None)
                    continue
            if target != previous.get(device["ip"]) or refresh:
                print(f"{device['side']} {device['ip']}: {json.dumps(target)}", flush=True)
                if not dry_run:
                    for command, data in govee_commands(target):
                        govee_send(device["ip"], command, data)
                        time.sleep(0.08)
                    changed.append(device)
                previous[device["ip"]] = target
        if refresh:
            last_refresh = time.monotonic()
        if once:
            if not dry_run:
                time.sleep(0.3)
                for device in changed:
                    status = govee_status(device["ip"])
                    print(f"Readback {device['ip']}: {json.dumps(status)}", flush=True)
                    if status is None:
                        raise ValueError(
                            "Govee did not confirm its state; physical result is unverified"
                        )
            return
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
            result = hue_request(config, "/api", {"devicetype": "smart-lighting#desktop"})
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
        config["light"] = args.light
        config["targets"] = (
            [{"ip": args.govee, "side": "center"}]
            if args.govee
            else [{"ip": args.left, "side": "left"}, {"ip": args.right, "side": "right"}]
        )
        save_config(config)
        print("Saved selection. Try sync --once --dry-run before sync --once.")
    elif args.command == "status":
        for device in config.get("targets", []):
            print(device["side"], device["ip"], json.dumps(govee_status(device["ip"])))
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
