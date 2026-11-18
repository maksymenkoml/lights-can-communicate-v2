"""Versioned adapter launcher; check mode never opens network endpoints."""

import argparse
import hmac
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys

from . import __version__, install, services
from .config import ConfigError, load_config, read_json
from .native_profile import NativeHueProfile, NativeProfileError


def _hue_identity(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value) is not None


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
    if config.hue_config_file and not config.native_profile_file:
        raise ConfigError("Hue output requires an explicit native Hue profile")
    try:
        profile = (
            NativeHueProfile.load(config.native_profile_file)
            if config.native_profile_file else None
        )
    except NativeProfileError:
        raise ConfigError("Invalid native Hue profile") from None
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
            if not _hue_identity(hue.get("hue_key")):
                raise ConfigError("Invalid Hue credential")
            live_adapter.HueStripProxy(live_adapter.PinnedHueClient(hue, profile=profile),
                                       profile=profile)
            if config.hue_play:
                for role, resource in profile.play_ids.items():
                    live_adapter.HueStripProxy(
                        live_adapter.PinnedHueClient(hue, resource_id=resource, profile=profile),
                        role=role, profile=profile,
                    )
        if config.hue_stream_config_file:
            stream = read_json(config.hue_stream_config_file, "Hue stream configuration")
            if not _hue_identity(stream.get("hue_key")) or (
                "application_id" in stream and not _hue_identity(stream["application_id"])
            ):
                raise ConfigError("Invalid native stream credential or identity")
            if stream.get("bridge") != hue.get("bridge"):
                raise ConfigError("Scene and stream Hue bridges must match")
            live_adapter.HueEntertainmentRelay(stream, profile=profile)
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


def _setup_module():
    """Setup Studio lives at the archive root or in the source tree."""
    try:
        import setup_studio
    except ImportError:
        import importlib.util
        from pathlib import Path

        source = Path(__file__).resolve().parents[1] / "experiments/setup-studio/server.py"
        spec = importlib.util.spec_from_file_location("setup_studio", source)
        setup_studio = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(setup_studio)
    return setup_studio


def _interruptible(run):
    previous = signal.getsignal(signal.SIGTERM)

    def terminate(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, terminate)
    try:
        return run()
    finally:
        signal.signal(signal.SIGTERM, previous)


def _adapter(config, check):
    runtime = prepare(config)
    if check:
        print(
            json.dumps(
                {
                    "version": __version__,
                    "profile": "explicit-native" if config.native_profile_file else "govee-only",
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
        # An intentional owner state, not a failure: supervisors must not loop.
        print("Owner live-output hold is active; adapter not started", file=sys.stderr)
        return 0
    _interruptible(lambda: runtime.main(
        config.arguments(),
        service_info={"name": "smart-lighting-adapter", "version": __version__},
    ))
    return 0


def _setup(config):
    _credential(config.token_file)
    studio = _setup_module()
    _interruptible(lambda: studio.serve(
        config.token_file, config.state_dir / "desk-layout.json",
        bind=config.setup.bind, port=config.setup.port,
        adapter_url=services.local_adapter_url(config),
    ))
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv and argv[0] in install.COMMANDS:
        return install.main(argv)
    parser = argparse.ArgumentParser(description="Smart Lighting managed core")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("command", nargs="?", default="adapter",
                        choices=("adapter", "setup", "ready", "services"))
    parser.add_argument("--config", required=True, help="Version-one service configuration")
    parser.add_argument(
        "--check", action="store_true", help="adapter: validate without network or state writes"
    )
    parser.add_argument("--release", help="services: installed release directory")
    parser.add_argument("--output", help="services: new directory for supervisor files")
    parser.add_argument("--timeout", type=float, default=10.0, help="ready: seconds to wait")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        if args.command == "adapter":
            return _adapter(config, args.check)
        if args.command == "setup":
            return _setup(config)
        if args.command == "ready":
            token = _credential(config.token_file).decode()
            result = services.ready(config, token, timeout=max(0.0, min(args.timeout, 300.0)))
            print(json.dumps(result))
            return 0 if result["ready"] else 1
        if not args.release or not args.output:
            raise ConfigError("services requires --release and --output")
        names = services.render(config, args.config, args.release, args.output, sys.executable)
        print(json.dumps({"rendered": names}))
        return 0
    except (ConfigError, OSError, ValueError) as error:
        message = (
            str(error) if isinstance(error, ConfigError) else "Command failed to start or stop"
        )
        print(message, file=sys.stderr)
        return 2
