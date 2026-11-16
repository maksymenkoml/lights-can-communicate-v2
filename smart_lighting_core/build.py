"""Build a deterministic executable archive from explicitly selected public code."""

import argparse
import hashlib
import json
import zipfile
from pathlib import Path

from . import __version__

PILOT_MODULES = (
    "live_adapter",
    "desk_layout",
    "entertainment_stream",
    "govee_segments",
    "hue_entertainment_relay",
    "hue_strip_proxy",
    "pilot_transport",
    "power_owner",
    "video_output",
    "bridge_output",
)
CORE_MODULES = (
    "__init__", "__main__", "config", "cli", "discovery", "hue_discovery", "live_preview",
)


def build(output):
    root = Path(__file__).resolve().parents[1]
    paths = {"smart_lighting.py": root / "smart_lighting.py"}
    paths.update(
        {name + ".py": root / "experiments/hue-bridge" / (name + ".py") for name in PILOT_MODULES}
    )
    paths.update(
        {
            "smart_lighting_core/" + name + ".py": root / "smart_lighting_core" / (name + ".py")
            for name in CORE_MODULES
        }
    )
    files = {}
    for name, path in paths.items():
        if path.is_symlink() or not path.is_file():
            raise ValueError("Missing or redirected runtime source")
        files[name] = path.read_bytes()
    files["__main__.py"] = b"from smart_lighting_core.cli import main\nraise SystemExit(main())\n"
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
