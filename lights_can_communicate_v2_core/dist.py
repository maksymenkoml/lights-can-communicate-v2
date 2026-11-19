"""Build a versioned release directory: archive, bridge modules and manifest."""

import argparse
import gzip
import hashlib
import io
import json
import shutil
import tarfile
from pathlib import Path

from . import __version__
from .build import build
from .config import GATEWAY_IMAGE
from .services import ARCHIVE, GATEWAY_MODULES

# Durable state formats this release writes. Rollback restores the
# pre-upgrade snapshot when these differ between two releases, so bump a
# number whenever a stored format changes.
STATE_SCHEMAS = {"desk_layout": 6, "homekit_power": 1, "scene_play_intent": 1}
CONFIG_VERSIONS = [1]
PYTHON = "3.10"
# Version of the signed /homekit API the bundled Homebridge plugin expects.
CORE_API = {"homekit": 1}
# npm's fixed package mtime (1985-10-26T08:15:00Z) keeps the tarball reproducible.
_NPM_MTIME = 499162500


def plugin_tarball(root, output_dir):
    """Write the Homebridge plugin as a deterministic npm tarball; return its metadata."""
    package_dir = root / "homebridge"
    package = json.loads((package_dir / "package.json").read_text())
    files = {"package.json"}
    if (package_dir / "README.md").is_file():
        files.add("README.md")
    for entry in package["files"]:
        path = package_dir / entry
        if path.is_dir():
            files.update(p.relative_to(package_dir).as_posix()
                         for p in path.rglob("*") if p.is_file())
        elif path.is_file():
            files.add(entry)
        else:
            raise ValueError("Missing plugin file")
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name in sorted(files):
            source = package_dir / name
            if source.is_symlink():
                raise ValueError("Redirected plugin file")
            data = source.read_bytes()
            info = tarfile.TarInfo("package/" + name)
            info.size, info.mode, info.mtime = len(data), 0o644, _NPM_MTIME
            tar.addfile(info, io.BytesIO(data))
    name = f"{package['name']}-{package['version']}.tgz"
    with (output_dir / name).open("xb") as raw, gzip.GzipFile(
            filename="", mode="wb", fileobj=raw, mtime=0) as stream:
        archive.seek(0)
        shutil.copyfileobj(archive, stream)
    return {"name": package["name"], "version": package["version"], "file": f"plugin/{name}"}


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
            source = root / "bridge" / (name + ".py")
            if source.is_symlink() or not source.is_file():
                raise ValueError("Missing or redirected gateway source")
            shutil.copyfile(source, gateway / (name + ".py"))
            (gateway / (name + ".py")).chmod(0o444)
        (output / "plugin").mkdir()
        plugin = plugin_tarball(root, output / "plugin")
        files = sorted(p.relative_to(output).as_posix() for p in output.rglob("*") if p.is_file())
        manifest = {
            "version": __version__,
            "compatibility": {
                "config_version": CONFIG_VERSIONS,
                "state": STATE_SCHEMAS,
                "python": PYTHON,
                "gateway_image": GATEWAY_IMAGE,
                "core_api": CORE_API,
                "plugin": plugin,
            },
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
