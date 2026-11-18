"""Build a versioned release directory: archive, bridge modules and manifest."""

import argparse
import hashlib
import json
import shutil
from pathlib import Path

from . import __version__
from .build import build
from .services import ARCHIVE, GATEWAY_MODULES


def build_release(output):
    """Create ``output`` exclusively; remove only what this call created on failure."""
    root = Path(__file__).resolve().parents[1]
    output = Path(output)
    output.mkdir(mode=0o755)
    try:
        build(output / ARCHIVE)
        gateway = output / "gateway"
        gateway.mkdir()
        for name in GATEWAY_MODULES:
            source = root / "experiments/hue-bridge" / (name + ".py")
            if source.is_symlink() or not source.is_file():
                raise ValueError("Missing or redirected gateway source")
            shutil.copyfile(source, gateway / (name + ".py"))
            (gateway / (name + ".py")).chmod(0o444)
        files = sorted(p.relative_to(output).as_posix() for p in output.rglob("*") if p.is_file())
        manifest = {
            "version": __version__,
            "files": {name: hashlib.sha256((output / name).read_bytes()).hexdigest()
                      for name in files},
        }
        with (output / "manifest.json").open("x") as stream:
            stream.write(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    except BaseException:
        shutil.rmtree(output, ignore_errors=True)
        raise
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        build_release(args.output)
    except (OSError, ValueError):
        parser.exit(2, "Cannot create release; use a new writable output path and valid sources\n")
    print(f"Built release {__version__}")


if __name__ == "__main__":
    main()
