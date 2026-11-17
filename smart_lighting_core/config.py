"""Strict, path-based service configuration; no device IO or state mutations."""

import ipaddress
import json
from dataclasses import dataclass
from pathlib import Path

MAX_BYTES = 65536


class ConfigError(ValueError):
    """A safe diagnostic that contains no input values or credentials."""


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ConfigError("Duplicate configuration key")
        result[key] = value
    return result


def read_json(path, label):
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ConfigError(f"Oversize {label}")
        result = json.loads(raw, object_pairs_hook=_unique)
        if not isinstance(result, dict):
            raise ConfigError(f"Expected object in {label}")
        return result
    except (OSError, ValueError, RecursionError) as error:
        raise ConfigError(f"Cannot read valid {label}") from error


def _fields(value, required, optional, label):
    if not isinstance(value, dict) or set(value) - required - optional or required - set(value):
        raise ConfigError(f"Invalid fields in {label}")


def _path(base, value, label):
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ConfigError(f"Invalid path for {label}")
    candidate = Path(value)
    return (candidate if candidate.is_absolute() else base / candidate).resolve()


@dataclass(frozen=True)
class ServiceConfig:
    state_dir: Path
    targets_file: Path
    token_file: Path
    power_owner_token_file: Path
    bind: str
    port: int
    gradient: bool
    hue_config_file: Path | None
    hue_play: bool
    hue_stream_config_file: Path | None
    native_profile_file: Path | None = None

    def arguments(self):
        result = [
            "--state-dir",
            str(self.state_dir),
            "--targets",
            str(self.targets_file),
            "--token-file",
            str(self.token_file),
            "--power-owner-token-file",
            str(self.power_owner_token_file),
            "--bind",
            self.bind,
            "--port",
            str(self.port),
        ]
        if self.hue_config_file:
            result.extend(["--hue-config", str(self.hue_config_file)])
        if self.hue_play:
            result.append("--hue-play")
        if self.hue_stream_config_file:
            result.extend(["--hue-stream-config", str(self.hue_stream_config_file)])
        if self.native_profile_file:
            result.extend(["--native-profile", str(self.native_profile_file)])
        return result


def load_config(path):
    path = Path(path).resolve()
    data = read_json(path, "service configuration")
    _fields(data, {"version", "state_dir", "adapter"}, set(), "service configuration")
    if type(data["version"]) is not int or data["version"] != 1:
        raise ConfigError("Unsupported service configuration version")
    options = data["adapter"]
    paths = {"targets_file", "token_file", "power_owner_token_file"}
    optional_paths = {"hue_config_file", "hue_stream_config_file", "native_profile_file"}
    _fields(options, paths, optional_paths | {"bind", "port", "gradient", "hue_play"}, "adapter")
    for field in ("gradient", "hue_play"):
        if type(options.get(field, False)) is not bool:
            raise ConfigError(f"Expected boolean {field}")
    port = options.get("port", 19880)
    if type(port) is not int or not 1 <= port <= 65535:
        raise ConfigError("Invalid adapter port")
    try:
        raw_bind = options.get("bind", "127.0.0.1")
        if not isinstance(raw_bind, str):
            raise ConfigError("Expected a textual bind address")
        bind = str(ipaddress.IPv4Address(raw_bind))
    except (ValueError, TypeError) as error:
        raise ConfigError("Invalid adapter bind address") from error
    address = ipaddress.IPv4Address(bind)
    if address.is_multicast or not (address.is_private or address.is_unspecified):
        raise ConfigError("Adapter bind must be local")
    resolved = {key: _path(path.parent, options[key], key) for key in paths}
    resolved.update(
        {
            key: _path(path.parent, options[key], key) if key in options else None
            for key in optional_paths
        }
    )
    if resolved["token_file"] == resolved["power_owner_token_file"]:
        raise ConfigError("Adapter and power credentials must be separate")
    if options.get("hue_play", False) and resolved["hue_config_file"] is None:
        raise ConfigError("Hue Play requires a Hue configuration")
    if resolved["native_profile_file"] and resolved["hue_config_file"] is None:
        raise ConfigError("Native Hue profile requires a Hue configuration")
    if resolved["hue_config_file"] and resolved["native_profile_file"] is None:
        raise ConfigError("Hue output requires an explicit native Hue profile")
    if resolved["hue_stream_config_file"] and not options.get("hue_play", False):
        raise ConfigError("Native video requires Hue Play")
    return ServiceConfig(
        state_dir=_path(path.parent, data["state_dir"], "state_dir"),
        bind=bind,
        port=port,
        gradient=options.get("gradient", False),
        hue_play=options.get("hue_play", False),
        **resolved,
    )
