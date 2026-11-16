"""Versioned adapter launcher; check mode never opens network endpoints."""

import argparse
import hmac
import json
import os
import shutil
import signal
import stat
import subprocess
import sys

from . import __version__
from .config import ConfigError, load_config, read_json


def _credential(path):
    try:
        with path.open("rb") as stream:
            if os.name == "posix" and stat.S_IMODE(os.fstat(stream.fileno()).st_mode) & 0o077:
                raise ConfigError("Credential file must be private to its owner")
            raw = stream.read(131)
    except OSError as error:
        raise ConfigError("Required credential file is unavailable") from error
    value = raw.strip()
    if (
        len(raw) > 130
        or not 32 <= len(value) <= 128
        or any(c not in b"0123456789abcdef" for c in value)
    ):
        raise ConfigError("Invalid local adapter credential")
    return value


def prepare(config):
    """Import and validate the existing runtime without starting any workers."""
    os.environ["PROBE_GOVEE_GRADIENT"] = "true" if config.gradient else "false"
    # Source mode explicitly adds only our known pilot module directory. In an
    # archive all runtime modules already live in the archive's import root.
    from pathlib import Path

    pilot = Path(__file__).resolve().parents[1] / "experiments" / "hue-bridge"
    if Path(__file__).is_file():
        sys.path.insert(0, str(pilot))
    import live_adapter

    import smart_lighting as lan

    token, owner = _credential(config.token_file), _credential(config.power_owner_token_file)
    if hmac.compare_digest(token, owner):
        raise ConfigError("Adapter and power credentials must differ")
    data = read_json(config.targets_file, "target configuration")
    try:
        targets = data["targets"]
        live_adapter.PilotController(targets)
        for target in targets:
            lan.local_ip(target["ip"])
            lan.parse_brightness_scale(target.get("brightness_scale", 1))
        hue = None
        if config.hue_config_file:
            hue = read_json(config.hue_config_file, "Hue configuration")
            if not isinstance(hue.get("hue_key"), str) or not 1 <= len(hue["hue_key"]) <= 128:
                raise ConfigError("Invalid Hue credential")
            live_adapter.HueStripProxy(live_adapter.PinnedHueClient(hue))
            if config.hue_play:
                for role, resource in live_adapter.PLAY_IDS.items():
                    live_adapter.HueStripProxy(
                        live_adapter.PinnedHueClient(hue, resource_id=resource), role=role
                    )
        if config.hue_stream_config_file:
            stream = read_json(config.hue_stream_config_file, "Hue stream configuration")
            if stream.get("bridge") != hue.get("bridge"):
                raise ConfigError("Scene and stream Hue bridges must match")
            live_adapter.HueEntertainmentRelay(stream)
            # Match the relay's binary selection without creating a DTLS client.
            binary = next(
                (
                    str(p)
                    for p in (
                        Path("/opt/homebrew/opt/openssl/bin/openssl"),
                        Path("/usr/local/opt/openssl/bin/openssl"),
                    )
                    if p.is_file()
                ),
                shutil.which("openssl"),
            )
            if not binary:
                raise ConfigError("Native video requires OpenSSL")
            version = subprocess.run(
                [binary, "version"], capture_output=True, timeout=3, check=True
            )
            if not version.stdout.startswith(b"OpenSSL "):
                raise ConfigError("Native video requires a supported OpenSSL runtime")
    except (
        KeyError,
        TypeError,
        ValueError,
        OverflowError,
        AttributeError,
        OSError,
        subprocess.SubprocessError,
    ) as error:
        raise ConfigError("Invalid adapter runtime configuration or dependency") from error
    return live_adapter


def main(argv=None):
    parser = argparse.ArgumentParser(description="Smart Lighting packaged desk adapter")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--config", required=True, help="Version-one service configuration")
    parser.add_argument(
        "--check", action="store_true", help="Validate without network or state writes"
    )
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        runtime = prepare(config)
        if args.check:
            print(
                json.dumps(
                    {
                        "version": __version__,
                        "profile": "pinned-desk",
                        "targets": 2,
                        "gradient": config.gradient,
                        "native_hue": bool(config.hue_config_file),
                        "native_video": bool(config.hue_stream_config_file),
                        "hold": (config.state_dir / "live-output-hold").exists(),
                    }
                )
            )
            return 0
        if (config.state_dir / "live-output-hold").exists():
            raise ConfigError("Owner live-output hold is active")
        previous = signal.getsignal(signal.SIGTERM)

        def terminate(signum, frame):
            raise KeyboardInterrupt

        signal.signal(signal.SIGTERM, terminate)
        try:
            runtime.main(
                config.arguments(),
                service_info={"name": "smart-lighting-adapter", "version": __version__},
            )
        finally:
            signal.signal(signal.SIGTERM, previous)
        return 0
    except (ConfigError, OSError, ValueError) as error:
        message = (
            str(error) if isinstance(error, ConfigError) else "Adapter failed to start or stop"
        )
        print(message, file=sys.stderr)
        return 2
