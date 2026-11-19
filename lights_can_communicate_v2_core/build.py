"""Build a deterministic executable archive from explicitly selected public code."""

import argparse
import hashlib
import json
import zipfile
from pathlib import Path

from . import __version__

RUNTIME_MODULES = (
    "live_adapter",
    "desk_layout",
    "entertainment_stream",
    "govee_segments",
    "govee_calibration",
    "calibration_session",
    "hue_entertainment_relay",
    "hue_strip_proxy",
    "govee_transport",
    "homekit_power",
    "power_history",
    "power_owner",
    "power_requests",
    "video_output",
    "bridge_output",
    "stream_http",
    "stream_diagnostics",
    "sync_health",
    "sync_projection",
    "sync_zone",
    "native_location_cache",
    "identify",
    "sync_off",
)
CORE_MODULES = (
    "__init__", "__main__", "config", "cli", "discovery", "hue_discovery", "live_preview",
    "native_profile", "services", "install",
)
SETUP_FILES = {
    "setup_studio.py": "server.py",
    "static/index.html": "static/index.html",
    "static/app.js": "static/app.js",
    "static/style.css": "static/style.css",
    "static/fonts/commissioner-latin.woff2": "static/fonts/commissioner-latin.woff2",
    "static/fonts/OFL.txt": "static/fonts/OFL.txt",
}


def build(output):
    root = Path(__file__).resolve().parents[1]
    paths = {"lights_can_communicate_v2.py": root / "lights_can_communicate_v2.py"}
    paths.update(
        {name + ".py": root / "bridge" / (name + ".py") for name in RUNTIME_MODULES}
    )
    paths.update(
        {
            "lights_can_communicate_v2_core/" + name + ".py":
                root / "lights_can_communicate_v2_core" / (name + ".py")
            for name in CORE_MODULES
        }
    )
    paths.update({name: root / "studio" / source
                  for name, source in SETUP_FILES.items()})
    files = {}
    for name, path in paths.items():
        if path.is_symlink() or not path.is_file():
            raise ValueError("Missing or redirected runtime source")
        files[name] = path.read_bytes()
    files["__main__.py"] = (
        b"from lights_can_communicate_v2_core.cli import main\nraise SystemExit(main())\n")
    files["core-manifest.json"] = (
        json.dumps(
            {
                "version": __version__,
                "files": {
                    name: hashlib.sha256(data).hexdigest() for name, data in sorted(files.items())
                },
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()
    # Exclusive creation preserves installed artifacts and rollback versions.
    with Path(output).open("xb") as target:
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_STORED) as archive:
            for name, data in sorted(files.items()):
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = 0o100444 << 16
                archive.writestr(info, data)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        build(args.output)
    except (OSError, ValueError):
        parser.exit(2, "Cannot create artifact; use a new writable output path and valid sources\n")
    print(f"Built adapter {__version__}")


if __name__ == "__main__":
    main()
