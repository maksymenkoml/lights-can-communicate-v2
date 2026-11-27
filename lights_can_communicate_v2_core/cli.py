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
import time

from . import __version__, services
from .config import ConfigError, load_config, read_json
from .light_registry import REGISTRY_FILE, RegistryError, ensure
from .native_profile import NativeProfileError


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


def prepare(config, *, write_registry):
    """Import and validate the existing runtime without starting any workers.

    The light registry is the only source of lights. A missing one is migrated
    from the old files in memory and written only after every check passed, so
    a configuration that fails to start never freezes a stale migration. With
    ``write_registry=False`` (configuration check) the state is never touched.
    Returns the runtime module and the light registry.
    """
    registry_path = config.state_dir / REGISTRY_FILE
    migrating = not (registry_path.exists() or registry_path.is_symlink())
    try:
        registry = ensure(config.state_dir, config.targets_file, config.native_profile_file)
        profile = registry.native_profile()
        hue_lights = registry.hue_lights()
    except RegistryError as error:
        raise ConfigError(str(error)) from None
    except NativeProfileError:
        raise ConfigError("Invalid native Hue profile") from None
    if hue_lights is not None and not config.hue_config_file:
        raise ConfigError("A Hue light in the light registry requires a Hue configuration")
    os.environ["PROBE_GOVEE_GRADIENT"] = "true" if config.gradient else "false"
    # Source mode explicitly adds only the known bridge module directory. In an
    # archive all runtime modules already live in the archive's import root.
    from pathlib import Path

    bridge_dir = Path(__file__).resolve().parents[1] / "bridge"
    if Path(__file__).is_file():
        sys.path.insert(0, str(bridge_dir))
    import live_adapter

    import lights_can_communicate_v2 as lan

    token, owner = _credential(config.token_file), _credential(config.power_owner_token_file)
    if hmac.compare_digest(token, owner):
        raise ConfigError("Adapter and power credentials must differ")
    try:
        targets = registry.govee_targets()
        live_adapter.GoveeController(targets)
        for target in targets:
            lan.local_ip(target["ip"])
            lan.parse_brightness_scale(target.get("brightness_scale", 1))
        hue = None
        if config.hue_config_file:
            hue = read_json(config.hue_config_file, "Hue configuration")
            if not _hue_identity(hue.get("hue_key")):
                raise ConfigError("Invalid Hue credential")
            # A paired bridge may have no Hue light yet, or none any more.
            for light, resource in (hue_lights.light_ids if hue_lights else {}).items():
                live_adapter.HueStripProxy(
                    live_adapter.PinnedHueClient(hue, resource_id=resource, profile=hue_lights),
                    light, profile=hue_lights,
                )
        if config.hue_stream_config_file:
            stream = read_json(config.hue_stream_config_file, "Hue stream configuration")
            if not _hue_identity(stream.get("hue_key")) or (
                "application_id" in stream and not _hue_identity(stream["application_id"])
            ):
                raise ConfigError("Invalid native stream credential or identity")
            if stream.get("bridge") != hue.get("bridge"):
                raise ConfigError("Scene and stream Hue bridges must match")
            if profile is not None:
                live_adapter.HueEntertainmentRelay(stream, profile=profile)
            # Match the relay's binary selection without creating a DTLS client.
            binary = shutil.which("openssl")
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
    if write_registry and migrating:
        try:
            registry.save(registry_path)
        except RegistryError as error:
            raise ConfigError(str(error)) from None
    return live_adapter, registry


def _setup_module():
    """Setup Studio lives at the archive root or in the source tree."""
    try:
        import setup_studio
    except ImportError:
        import importlib.util
        from pathlib import Path

        source = Path(__file__).resolve().parents[1] / "studio/server.py"
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


HOLD_POLL_SECONDS = 5.0


def _adapter(config, check, sleep=time.sleep):
    runtime, registry = prepare(config, write_registry=not check)
    if check:
        print(
            json.dumps(
                {
                    "version": __version__,
                    "profile": "explicit-native" if registry.native_profile() else "govee-only",
                    "targets": len(registry.govee_targets()),
                    "gradient": config.gradient,
                    "native_hue": bool(config.hue_config_file),
                    "native_video": bool(config.hue_stream_config_file),
                    "hold": (config.state_dir / "live-output-hold").exists(),
                }
            )
        )
        return 0
    hold = config.state_dir / "live-output-hold"
    if hold.exists():
        # An intentional owner state, not a failure: supervisors must not loop.
        print("Owner live-output hold is active; adapter not started", file=sys.stderr)
        # Docker restarts a finished container whatever its exit code, so wait
        # here until the owner removes the hold; SIGTERM still stops the wait.
        while hold.exists():
            sleep(HOLD_POLL_SECONDS)
        print("Owner live-output hold removed; starting adapter", file=sys.stderr)
    _interruptible(lambda: runtime.main(
        config.arguments(),
        service_info={"name": "lights-can-communicate-v2-adapter", "version": __version__},
    ))
    return 0


def _setup(config):
    _credential(config.token_file)
    studio = _setup_module()
    urls = services.endpoints(config)
    _interruptible(lambda: studio.serve(
        config.token_file, config.state_dir,
        bind=config.setup.bind, port=config.setup.port,
        adapter_url=urls["adapter"],
        bridge_url=urls["bridge"],
        # The LAN address always names Light Studio, whatever STUDIO_HOSTS adds.
        hosts=(config.gateway.lan_ip, config.container.core_address,
               *config.container.studio_hosts),
        hue_config=config.hue_config_file,
    ))
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    parser = argparse.ArgumentParser(description="Lights Can Communicate v2 managed core")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("command", nargs="?", default="adapter",
                        choices=("adapter", "setup", "container"))
    parser.add_argument("--config", help="Version-two service configuration")
    parser.add_argument(
        "--check", action="store_true", help="adapter: validate without network or state writes"
    )
    parser.add_argument("--data", default="/data", help="container: the data folder")
    args = parser.parse_args(argv)
    try:
        if args.command == "container":
            from . import container

            return container.run(args.data)
        if not args.config:
            raise ConfigError("This command requires --config")
        config = load_config(args.config)
        if args.command == "adapter":
            return _adapter(config, args.check)
        return _setup(config)
    except (ConfigError, OSError, ValueError) as error:
        message = (
            str(error) if isinstance(error, ConfigError) else "Command failed to start or stop"
        )
        print(message, file=sys.stderr)
        return 2
