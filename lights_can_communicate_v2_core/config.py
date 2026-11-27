"""Strict, path-based service configuration; no device IO or state mutations."""

import ipaddress
import json
import re
import zoneinfo
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .light_registry import REGISTRY_FILE

# The Hue bridge that Light Studio paired (bridge/hue_bridge_pairing.py).
PAIRED_HUE_FILE = "hue-bridge.json"

MAX_BYTES = 65536
GATEWAY_IMAGE = (
    "diyhue/core@sha256:392c1c39a5b46168d1d6d8582abd266586bdd45d61fb3459fa95821c710ffcba"
)
_MAC = re.compile(r"[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){5}\Z")
_TIMEZONE = re.compile(r"[A-Za-z_]+(/[A-Za-z0-9_+-]+){0,2}\Z")
_LABEL = r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"
_HOSTNAME = re.compile(rf"(?=.{{1,253}}\Z){_LABEL}(\.{_LABEL})*\Z")
# The adapter's address on the container's internal network; Homebridge on the
# NAS host reaches the adapter and the bridge here.
CORE_ADDRESS = "172.30.250.3"
DEFAULT_TIMEZONE = "Etc/UTC"
VERSION = 2


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
    bind: str = "0.0.0.0"
    port: int = 19881


@dataclass(frozen=True)
class GatewayConfig:
    lan_ip: str
    mac: str
    adapter_url: str
    live_output: bool = False
    hue_strip: bool = False
    hue_play: bool = False
    entertainment: bool = False
    gradient: bool = False
    timezone: str = DEFAULT_TIMEZONE

    def environment(self, config_path="/opt/hue-emulator/config"):
        """The gateway's environment; ``config_path`` is the directory diyHue uses."""
        flag = {True: "true", False: "false"}
        return {
            "IP": self.lan_ip,
            "MAC": self.mac,
            "CONFIG_PATH": str(config_path),
            "DEBUG": "false",
            "TZ": self.timezone,
            "PROBE_LIVE_OUTPUT": flag[self.live_output],
            "PROBE_ADAPTER_URL": self.adapter_url,
            "PROBE_HUE_STRIP": flag[self.hue_strip],
            "PROBE_HUE_PLAY": flag[self.hue_play],
            "PROBE_ENTERTAINMENT": flag[self.entertainment],
            "PROBE_GOVEE_GRADIENT": flag[self.gradient],
        }


@dataclass(frozen=True)
class ContainerConfig:
    """The container profile: one LAN address, one internal address.

    The LAN address is ``gateway.lan_ip``. ``core_address`` is the fixed address on
    the container's internal network, where the adapter listens and from where the
    NAS host reaches the adapter and the bridge.
    """

    core_address: str = CORE_ADDRESS
    studio_hosts: tuple = ()


def timezone_name(value):
    """Return an IANA time zone name that this system knows, else raise ConfigError."""
    if not isinstance(value, str) or not _TIMEZONE.match(value):
        raise ConfigError("Invalid gateway timezone")
    try:
        zoneinfo.ZoneInfo(value)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError, OSError):
        raise ConfigError("Unknown gateway timezone") from None
    return value


def studio_host_names(names):
    """Validate and lower-case Light Studio host names; keep the first of each."""
    if not isinstance(names, list) or len(names) > 8:
        raise ConfigError("Container studio hosts must be a short list")
    hosts = []
    for name in names:
        if not isinstance(name, str) or not _HOSTNAME.match(name.lower()):
            raise ConfigError("Invalid container studio host")
        if name.lower() not in hosts:
            hosts.append(name.lower())
    return hosts


def _container(value):
    _fields(value, set(), {"core_address", "studio_hosts"}, "container")
    core_address = _address(value.get("core_address", CORE_ADDRESS), "container core address")
    if ipaddress.IPv4Address(core_address).is_unspecified:
        raise ConfigError("Container core address must be specific")
    return ContainerConfig(core_address=core_address,
                           studio_hosts=tuple(studio_host_names(value.get("studio_hosts", []))))


def _setup(value):
    _fields(value, set(), {"bind", "port"}, "setup")
    bind = _address(value.get("bind", "0.0.0.0"), "setup bind address")
    if bind != "0.0.0.0":
        # The container publishes Light Studio's port on every interface.
        raise ConfigError("Container profile serves Light Studio on all interfaces")
    return SetupConfig(bind=bind, port=_port(value.get("port", 19881), "setup port"))


def _gateway(value, adapter_host, adapter_port):
    flags = ("live_output", "hue_strip", "hue_play", "entertainment", "gradient")
    _fields(value, {"lan_ip", "mac"}, {"adapter_url", "timezone", *flags}, "gateway")
    for field in flags:
        if type(value.get(field, False)) is not bool:
            raise ConfigError(f"Expected boolean {field}")
    if value.get("entertainment", False) and not (
            value.get("hue_strip", False) and value.get("hue_play", False)):
        # The gateway builds Desk Entertainment from all six virtual lights and
        # refuses to start without them; the supervisor would restart it forever.
        raise ConfigError("Gateway entertainment requires hue_strip and hue_play")
    lan_ip = _address(value["lan_ip"], "gateway LAN address")
    if ipaddress.IPv4Address(lan_ip).is_unspecified:
        raise ConfigError("Gateway LAN address must be specific")
    if not isinstance(value["mac"], str) or not _MAC.match(value["mac"]):
        raise ConfigError("Invalid gateway MAC address")
    timezone = timezone_name(value.get("timezone", DEFAULT_TIMEZONE))
    url = value.get("adapter_url", f"http://{adapter_host}:{adapter_port}")
    parsed = urlsplit(url) if isinstance(url, str) else None
    if (parsed is None or parsed.scheme != "http" or not parsed.hostname
            or parsed.username or parsed.password or parsed.path not in ("", "/")
            or parsed.query or parsed.fragment):
        raise ConfigError("Invalid gateway adapter URL")
    return GatewayConfig(lan_ip=lan_ip, mac=value["mac"].lower(), adapter_url=url.rstrip("/"),
                         timezone=timezone, **{field: value.get(field, False) for field in flags})


@dataclass(frozen=True)
class ServiceConfig:
    state_dir: Path
    targets_file: Path | None
    token_file: Path
    power_owner_token_file: Path
    bind: str
    port: int
    gradient: bool
    hue_config_file: Path | None
    hue_play: bool
    hue_stream_config_file: Path | None
    gateway: GatewayConfig
    container: ContainerConfig = ContainerConfig()
    native_profile_file: Path | None = None
    power_authority: str = "owner"
    setup: SetupConfig = SetupConfig()

    @property
    def registry_file(self):
        return self.state_dir / REGISTRY_FILE

    def arguments(self):
        result = [
            "--state-dir",
            str(self.state_dir),
            "--registry",
            str(self.registry_file),
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
        # The one LAN address of the core: LAN searches send from here.
        result.extend(["--lan-address", self.gateway.lan_ip])
        result.extend(["--power-authority", self.power_authority])
        return result


def load_config(path):
    path = Path(path).resolve()
    data = read_json(path, "service configuration")
    _fields(data, {"version", "state_dir", "adapter", "gateway"}, {"setup", "container"},
            "service configuration")
    if type(data["version"]) is not int or data["version"] != VERSION:
        raise ConfigError("Unsupported service configuration version")
    options = data["adapter"]
    paths = {"token_file", "power_owner_token_file"}
    # The targets file and the native profile only seed the one-time light
    # registry migration; once the registry exists they are not read.
    optional_paths = {"targets_file", "hue_config_file", "hue_stream_config_file",
                      "native_profile_file"}
    _fields(options, paths,
            optional_paths | {"bind", "port", "gradient", "hue_play", "power_authority"},
            "adapter")
    if options.get("power_authority", "owner") not in ("owner", "homekit"):
        raise ConfigError("Invalid power authority")
    for field in ("gradient", "hue_play"):
        if type(options.get(field, False)) is not bool:
            raise ConfigError(f"Expected boolean {field}")
    container = _container(data.get("container", {}))
    port = _port(options.get("port", 19880), "adapter port")
    bind = _address(options.get("bind", container.core_address), "adapter bind address")
    if bind != container.core_address:
        # Homebridge on the NAS host reaches the adapter on the core address only.
        raise ConfigError("Container profile binds the adapter to its core address")
    setup = _setup(data.get("setup", {}))
    gateway = _gateway(data["gateway"], container.core_address, port)
    resolved = {key: _path(path.parent, options[key], key) for key in paths}
    resolved.update(
        {
            key: _path(path.parent, options[key], key) if key in options else None
            for key in optional_paths
        }
    )
    state_dir = _path(path.parent, data["state_dir"], "state_dir")
    if resolved["token_file"] == resolved["power_owner_token_file"]:
        raise ConfigError("Adapter and power credentials must be separate")
    if options.get("hue_play", False) and resolved["hue_config_file"] is None:
        raise ConfigError("Hue Play requires a Hue configuration")
    if resolved["hue_stream_config_file"] and not options.get("hue_play", False):
        raise ConfigError("Native video requires Hue Play")
    hue_play = options.get("hue_play", False)
    # Without Hue files of its own, the service uses the bridge that Light Studio
    # paired, for scenes and for the Sync stream. Explicit files always win.
    paired = state_dir / PAIRED_HUE_FILE
    if (resolved["hue_config_file"] is None and resolved["hue_stream_config_file"] is None
            and paired.is_file()):
        resolved["hue_config_file"] = resolved["hue_stream_config_file"] = paired
        hue_play = True
    return ServiceConfig(
        state_dir=state_dir,
        bind=bind,
        port=port,
        gradient=options.get("gradient", False),
        hue_play=hue_play,
        power_authority=options.get("power_authority", "owner"),
        setup=setup,
        gateway=gateway,
        container=container,
        **resolved,
    )
