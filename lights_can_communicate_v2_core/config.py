"""Strict, path-based service configuration; no device IO or state mutations."""

import ipaddress
import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

MAX_BYTES = 65536
GATEWAY_IMAGE = (
    "diyhue/core@sha256:392c1c39a5b46168d1d6d8582abd266586bdd45d61fb3459fa95821c710ffcba"
)
_IMAGE = re.compile(r"diyhue/core@sha256:[0-9a-f]{64}\Z")
_MAC = re.compile(r"[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){5}\Z")
_CONTAINER = re.compile(r"[a-z0-9][a-z0-9_.-]{0,62}\Z")
_TIMEZONE = re.compile(r"[A-Za-z_]+(/[A-Za-z0-9_+-]+){0,2}\Z")


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


def _address(value, label):
    try:
        if not isinstance(value, str):
            raise ConfigError(f"Expected a textual {label}")
        address = ipaddress.IPv4Address(value)
    except (ValueError, TypeError) as error:
        raise ConfigError(f"Invalid {label}") from error
    if address.is_multicast or not (address.is_private or address.is_unspecified):
        raise ConfigError(f"{label.capitalize()} must be local")
    return str(address)


def _port(value, label):
    if type(value) is not int or not 1 <= value <= 65535:
        raise ConfigError(f"Invalid {label}")
    return value


@dataclass(frozen=True)
class SetupConfig:
    bind: str = "127.0.0.1"
    port: int = 19881


@dataclass(frozen=True)
class GatewayConfig:
    lan_ip: str
    mac: str
    adapter_url: str
    container_name: str = "lights-can-communicate-v2-gateway"
    image: str = GATEWAY_IMAGE
    live_output: bool = False
    hue_strip: bool = False
    hue_play: bool = False
    entertainment: bool = False
    gradient: bool = False
    timezone: str = "America/Toronto"

    def environment(self):
        flag = {True: "true", False: "false"}
        return {
            "IP": self.lan_ip,
            "MAC": self.mac,
            "CONFIG_PATH": "/opt/hue-emulator/config",
            "DEBUG": "false",
            "TZ": self.timezone,
            "PROBE_LIVE_OUTPUT": flag[self.live_output],
            "PROBE_ADAPTER_URL": self.adapter_url,
            "PROBE_HUE_STRIP": flag[self.hue_strip],
            "PROBE_HUE_PLAY": flag[self.hue_play],
            "PROBE_ENTERTAINMENT": flag[self.entertainment],
            "PROBE_GOVEE_GRADIENT": flag[self.gradient],
        }


def _setup(value):
    _fields(value, set(), {"bind", "port"}, "setup")
    return SetupConfig(bind=_address(value.get("bind", "127.0.0.1"), "setup bind address"),
                       port=_port(value.get("port", 19881), "setup port"))


def _gateway(value, adapter_port):
    flags = ("live_output", "hue_strip", "hue_play", "entertainment", "gradient")
    _fields(value, {"lan_ip", "mac"},
            {"container_name", "image", "adapter_url", "timezone", *flags}, "gateway")
    for field in flags:
        if type(value.get(field, False)) is not bool:
            raise ConfigError(f"Expected boolean {field}")
    lan_ip = _address(value["lan_ip"], "gateway LAN address")
    if ipaddress.IPv4Address(lan_ip).is_unspecified:
        raise ConfigError("Gateway LAN address must be specific")
    if not isinstance(value["mac"], str) or not _MAC.match(value["mac"]):
        raise ConfigError("Invalid gateway MAC address")
    name = value.get("container_name", "lights-can-communicate-v2-gateway")
    if not isinstance(name, str) or not _CONTAINER.match(name):
        raise ConfigError("Invalid gateway container name")
    image = value.get("image", GATEWAY_IMAGE)
    if not isinstance(image, str) or not _IMAGE.match(image):
        raise ConfigError("Gateway image must be a pinned diyHue digest")
    timezone = value.get("timezone", "America/Toronto")
    if not isinstance(timezone, str) or not _TIMEZONE.match(timezone):
        raise ConfigError("Invalid gateway timezone")
    url = value.get("adapter_url", f"http://host.docker.internal:{adapter_port}")
    parsed = urlsplit(url) if isinstance(url, str) else None
    if (parsed is None or parsed.scheme != "http" or not parsed.hostname
            or parsed.username or parsed.password or parsed.path not in ("", "/")
            or parsed.query or parsed.fragment):
        raise ConfigError("Invalid gateway adapter URL")
    return GatewayConfig(lan_ip=lan_ip, mac=value["mac"].lower(), adapter_url=url.rstrip("/"),
                         container_name=name, image=image, timezone=timezone,
                         **{field: value.get(field, False) for field in flags})


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
    power_authority: str = "owner"
    setup: SetupConfig = SetupConfig()
    gateway: GatewayConfig | None = None

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
        result.extend(["--power-authority", self.power_authority])
        return result


def load_config(path):
    path = Path(path).resolve()
    data = read_json(path, "service configuration")
    _fields(data, {"version", "state_dir", "adapter"}, {"setup", "gateway"},
            "service configuration")
    if type(data["version"]) is not int or data["version"] != 1:
        raise ConfigError("Unsupported service configuration version")
    options = data["adapter"]
    paths = {"targets_file", "token_file", "power_owner_token_file"}
    optional_paths = {"hue_config_file", "hue_stream_config_file", "native_profile_file"}
    _fields(options, paths,
            optional_paths | {"bind", "port", "gradient", "hue_play", "power_authority"},
            "adapter")
    if options.get("power_authority", "owner") not in ("owner", "homekit"):
        raise ConfigError("Invalid power authority")
    for field in ("gradient", "hue_play"):
        if type(options.get(field, False)) is not bool:
            raise ConfigError(f"Expected boolean {field}")
    port = _port(options.get("port", 19880), "adapter port")
    bind = _address(options.get("bind", "127.0.0.1"), "adapter bind address")
    setup = _setup(data.get("setup", {}))
    gateway = _gateway(data["gateway"], port) if "gateway" in data else None
    if gateway is not None and ipaddress.IPv4Address(bind).is_loopback:
        # A container reaches the host through its network, never its loopback.
        raise ConfigError("Gateway requires an adapter bind reachable from its container")
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
        power_authority=options.get("power_authority", "owner"),
        setup=setup,
        gateway=gateway,
        **resolved,
    )
