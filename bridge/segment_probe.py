"""Isolated segment probe of a segmented Govee light; requires the running adapter to be stopped."""

import argparse
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from govee_models import capabilities  # noqa: E402
from govee_segments import interpolate, segment_data  # noqa: E402

import lights_can_communicate_v2 as lan  # noqa: E402
from lights_can_communicate_v2_core.light_registry import LightRegistry  # noqa: E402


def fresh_on(device, targets):
    for _ in range(4):
        status = lan.verified_status(device, timeout=0.3, targets=targets)
        if status is not None:
            return status if status.get("onOff") == 1 else None
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--light", required=True, help="registry light id, e.g. govee-left")
    parser.add_argument("--hold", action="store_true")
    parser.add_argument("--reverse", action="store_true")
    parser.add_argument("--batch-only", action="store_true")
    args = parser.parse_args()
    targets = LightRegistry.load(args.registry).govee_targets()
    if len({lan.device_key(item) for item in targets}) != len(targets):
        raise SystemExit("Pinned device IDs must be distinct")
    with socket.socket() as sock:
        sock.settimeout(0.2)
        if sock.connect_ex(("127.0.0.1", 19880)) == 0:
            raise SystemExit("Stop the live adapter before this isolated probe")
    device = next((item for item in targets if item["light"] == args.light), None)
    if device is None:
        raise SystemExit("Unknown Govee light id")
    status = fresh_on(device, targets)
    if status is None or status.get("onOff") != 1:
        raise SystemExit("Selected lamp is off or its identity could not be verified")
    ip = lan.GOVEE_ROUTES[lan.device_key(device)]
    identity = lan.govee_scan_at(ip)
    if not identity or capabilities(identity.get("sku"))["tier"] != "segmented":
        raise SystemExit("Segment probe requires a verified segmented model")
    colors = interpolate([(120, 0, 0), (0, 120, 0), (0, 0, 120)])
    if args.reverse:
        colors.reverse()
    try:
        # Establish each zone with the single-frame format used by the reference.
        packets = (
            [segment_data(colors)]
            if args.batch_only
            else [{"command": [frame]} for frame in segment_data(colors)["command"]]
        )
        for packet in packets:
            fresh = fresh_on(device, targets)
            if not fresh or fresh.get("onOff") != 1:
                raise SystemExit("Owner off or lost identity; probe stopped")
            if not lan.send_to_target(device, "ptReal", packet):
                raise SystemExit("Verified route expired; probe stopped")
            time.sleep(0.15)
        print(f"{args.light}: segment pattern sent; reverse={args.reverse}", flush=True)
        if args.hold:
            print("Holding test pattern until interrupted (maximum ten minutes).", flush=True)
            deadline = time.monotonic() + 600
            while time.monotonic() < deadline:
                time.sleep(0.5)
            return
        time.sleep(8)
        fresh = fresh_on(device, targets)
        if fresh and fresh.get("onOff") == 1:
            sent = lan.send_to_target(device, "ptReal", segment_data(list(reversed(colors))))
            print(
                f"{args.light}: batch sent={sent}; expected blue bottom / red top",
                flush=True,
            )
        time.sleep(25)
    finally:
        fresh = fresh_on(device, targets)
        if fresh and fresh.get("onOff") == 1:
            data = {
                "color": status.get("color") or {"r": 0, "g": 0, "b": 0},
                "colorTemInKelvin": status.get("colorTemInKelvin", 0),
            }
            restored = lan.send_to_target(device, "colorwc", data)
            print(
                f"{args.light}: previous whole-lamp color restored={restored}",
                flush=True,
            )
        else:
            print(f"{args.light}: restoration skipped; lamp off or identity unavailable",
                  flush=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Segment probe stopped.", flush=True)
