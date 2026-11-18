"""Render supervisor definitions and check readiness; never controls services."""

import ipaddress
import json
import plistlib
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import __version__
from .config import ConfigError

ADAPTER_LABEL = "com.smart-lighting.adapter"
SETUP_LABEL = "com.smart-lighting.setup"
ARCHIVE = "smart-lighting.pyz"
GATEWAY_MODULES = (
    "bootstrap",
    "bridge_output",
    "power_feedback",
    "hue_power_requests",
    "hue_events",
    "entertainment_stream",
    "entertainment_receiver",
    "udp_port_guard",
    "stream_http",
    "stream_diagnostics",
    "scene_play_intent",
    "gradient_profile",
    "hue_resource_compat",
    "desk_layout",
    "sync_projection",
    "sync_zone",
)
GATEWAY_PORTS = ("80:80/tcp", "443:443/tcp", "1900:1900/udp", "2100:2100/udp")
_HEALTH = ("import urllib.request;"
           "urllib.request.urlopen('http://127.0.0.1/api/config', timeout=3)")


def local_adapter_url(config):
    """Loopback when the adapter listens on all interfaces, else its own address."""
    host = "127.0.0.1" if ipaddress.IPv4Address(config.bind).is_unspecified else config.bind
    return f"http://{host}:{config.port}"


def _release(release):
    release = Path(release).resolve()
    try:
        manifest = json.loads((release / "manifest.json").read_text())
    except (OSError, ValueError) as error:
        raise ConfigError("Release manifest is unavailable") from error
    if not isinstance(manifest, dict) or manifest.get("version") != __version__:
        raise ConfigError("Release version differs from this launcher")
    required = [ARCHIVE] + [f"gateway/{name}.py" for name in GATEWAY_MODULES]
    if any(not (release / name).is_file() or (release / name).is_symlink() for name in required):
        raise ConfigError("Release is incomplete")
    return release


def agent(label, command, config, config_path, release, python):
    log = config.logs_dir / f"{command}.log"
    return {
        "Label": label,
        "ProgramArguments": [str(python), "-I", "-B", str(release / ARCHIVE), command,
                             "--config", str(config_path)],
        "WorkingDirectory": str(config.state_dir),
        "RunAtLoad": True,
        # Restart after crashes only; an orderly stop or owner hold exits 0.
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 10,
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
    }


def gateway_compose(config, release):
    gateway = config.gateway
    if gateway is None:
        raise ConfigError("Gateway configuration is required to render its service")
    volumes = [f"{config.state_dir}:/opt/hue-emulator/config"]
    volumes += [f"{release / 'gateway' / (name + '.py')}:/probe/{name}.py:ro"
                for name in GATEWAY_MODULES]
    return {
        "name": "smart-lighting",
        "services": {
            "gateway": {
                "image": gateway.image,
                "container_name": gateway.container_name,
                "restart": "unless-stopped",
                "entrypoint": ["python3", "-u", "/probe/bootstrap.py", "--docker"],
                "environment": gateway.environment(),
                "extra_hosts": ["host.docker.internal:host-gateway"],
                "ports": list(GATEWAY_PORTS),
                "volumes": volumes,
                "stop_grace_period": "20s",
                "healthcheck": {
                    "test": ["CMD", "python3", "-c", _HEALTH],
                    "interval": "30s",
                    "timeout": "5s",
                    "retries": 3,
                    "start_period": "30s",
                },
            }
        },
    }


def render(config, config_path, release, output, python):
    """Write supervisor files into a new directory; never overwrite."""
    release = _release(release)
    files = {
        f"{ADAPTER_LABEL}.plist": plistlib.dumps(
            agent(ADAPTER_LABEL, "adapter", config, Path(config_path).resolve(), release, python)),
        f"{SETUP_LABEL}.plist": plistlib.dumps(
            agent(SETUP_LABEL, "setup", config, Path(config_path).resolve(), release, python)),
    }
    if config.gateway is not None:
        files["gateway-compose.json"] = (
            json.dumps(gateway_compose(config, release), indent=2) + "\n").encode()
    output = Path(output)
    output.mkdir(mode=0o700)
    for name, data in files.items():
        with (output / name).open("xb") as stream:
            stream.write(data)
    return sorted(files)


def _get(url, headers=None, timeout=1.0):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with opener.open(request, timeout=timeout) as response:
            if response.status != 200:
                return None
            data = json.loads(response.read(1 << 20))
            return data if isinstance(data, dict) else None
    except urllib.error.HTTPError as error:
        error.close()
        return None
    except (OSError, ValueError):
        return None


def ready(config, token, timeout=10.0, clock=time.monotonic, sleep=time.sleep):
    """Return a credential-free readiness summary once components answer locally."""
    deadline = clock() + timeout
    while True:
        status = _get(local_adapter_url(config) + "/status", {"X-Adapter-Token": token})
        bridge = _get("http://127.0.0.1/api/config") if config.gateway is not None else {}
        service = status.get("service") if isinstance(status, dict) else None
        result = {
            "adapter": status is not None,
            "adapter_version": service.get("version") if isinstance(service, dict) else None,
            "gateway": None if config.gateway is None else bridge is not None,
        }
        if result["adapter"] and result["gateway"] is not False:
            result["ready"] = True
            return result
        if clock() >= deadline:
            result["ready"] = False
            return result
        sleep(0.5)
