import collections
import io
import json
import statistics
import sys
import time
from contextlib import redirect_stdout
from pathlib import Path

import lights_can_communicate_v2 as lighting


class MeasurementDone(Exception):
    pass


config = lighting.load_config()
original_send = lighting.govee_send
original_hue = lighting.hue_request
original_status = lighting.govee_status
source = original_hue(config, "/clip/v2/resource/light/" + config["light"])["data"][0]
if not lighting.animation_settings(source):
    raise SystemExit("Enable Hue dynamic Play before measuring")
sent = collections.defaultdict(list)
requests = []
statuses = []
started = time.monotonic()


def send(ip, command, data):
    if command == "colorwc":
        sent[ip].append((time.monotonic() - started, data["color"]))
    return original_send(ip, command, data)


def hue(*args, **kwargs):
    if time.monotonic() - started >= 15:
        raise MeasurementDone
    begin = time.monotonic()
    result = original_hue(*args, **kwargs)
    requests.append(time.monotonic() - begin)
    if not lighting.animation_settings(result["data"][0]):
        raise SystemExit("Scene changed out of dynamic Play during measurement")
    return result


def status(*args, **kwargs):
    begin = time.monotonic()
    result = original_status(*args, **kwargs)
    statuses.append({"seconds": time.monotonic() - begin, "replied": result is not None})
    return result


lighting.govee_send = send
lighting.hue_request = hue
lighting.govee_status = status
try:
    with redirect_stdout(io.StringIO()):
        lighting.run_sync(config)
except MeasurementDone:
    pass
result = {
    "measurement": sys.argv[1],
    "duration_seconds": round(time.monotonic() - started, 3),
    "hue_request_seconds": requests,
    "govee_status": statuses,
    "source_speed": source["dynamics"]["speed"],
    "devices": {},
}
for target in config["targets"]:
    frames = sent[target["ip"]]
    gaps = [b[0] - a[0] for a, b in zip(frames, frames[1:])]
    jumps = [
        max(abs(b[1][key] - a[1][key]) for key in ("r", "g", "b"))
        for a, b in zip(frames, frames[1:])
    ]
    result["devices"][target["side"]] = {
        "frames": len(frames),
        "timestamps_seconds": [item[0] for item in frames],
        "median_gap_ms": round(statistics.median(gaps) * 1000, 2),
        "p95_gap_ms": round(sorted(gaps)[int((len(gaps) - 1) * 0.95)] * 1000, 2),
        "max_gap_ms": round(max(gaps) * 1000, 2),
        "max_channel_step": max(jumps),
    }
folder = Path("archive/smoothing")
folder.mkdir(parents=True, exist_ok=True)
(folder / (sys.argv[1] + ".json")).write_text(json.dumps(result, indent=2) + "\n")
print(
    json.dumps(
        {
            key: value
            for key, value in result.items()
            if key not in ("govee_status", "hue_request_seconds")
        },
        indent=2,
    )
)
print("Hue request median ms:", round(statistics.median(requests) * 1000, 2))
print(
    "Govee status median ms:",
    round(statistics.median(item["seconds"] for item in statuses) * 1000, 2),
)
