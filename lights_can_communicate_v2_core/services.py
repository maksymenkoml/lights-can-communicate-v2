"""Render supervisor definitions and check readiness; never controls services."""

import ipaddress
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import __version__
from .config import ConfigError

PROJECT = "lights-can-communicate-v2"
ADAPTER_CONTAINER = f"{PROJECT}-adapter"
STUDIO_CONTAINER = f"{PROJECT}-studio"
ARCHIVE = "lights-can-communicate-v2.pyz"
COMPOSE = "compose.json"
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
    "hue_names",
    "desk_layout",
    "sync_projection",
    "sync_zone",
)
_HEALTH = ("import urllib.request;"
           "urllib.request.urlopen('http://127.0.0.1/api/config', timeout=3)")


def endpoints(config):
    """Where the host and Light Studio reach each component.

    They use the fixed core network: a host cannot reach its own macvlan
    containers, and their loopback is not the host's.
    """
    container = config.container
    return {
        "adapter": f"http://{container.adapter_core}:{config.port}",
        "setup": (container.studio_core, config.setup.port),
        "bridge": f"http://{container.gateway_core}",
    }


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


def _gateway_service(config, release):
    gateway = config.gateway
    volumes = [f"{config.state_dir}:/opt/hue-emulator/config"]
    volumes += [f"{release / 'gateway' / (name + '.py')}:/probe/{name}.py:ro"
                for name in GATEWAY_MODULES]
    return {
        "image": gateway.image,
        "container_name": gateway.container_name,
        "restart": "unless-stopped",
        "entrypoint": ["python3", "-u", "/probe/bootstrap.py", "--docker"],
        "environment": gateway.environment(),
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


def container_compose(config, config_path, release):
    """The NAS profile: gateway, adapter and studio in one project.

    The gateway and the adapter get their own LAN addresses (macvlan): Hue
    clients need TCP 80/443, which the NAS holds, and Govee replies arrive on
    UDP 4002, which the Govee plugin holds. All three share a fixed core network
    that the host, Homebridge and Light Studio use to reach them.
    """
    container = config.container
    gateway = _gateway_service(config, release)
    gateway["networks"] = {
        "lan": {"ipv4_address": config.gateway.lan_ip},
        "core": {"ipv4_address": container.gateway_core},
    }
    config_dir = Path(config_path).resolve().parent
    # Same paths inside and outside, so one configuration serves the host and
    # every container; the release path goes through the installation's link.
    volumes = [f"{config_dir}:{config_dir}:ro", f"{config.state_dir}:{config.state_dir}",
               f"{release}:{release}:ro"]

    def core(name, command, networks, **extra):
        return {
            "image": config.gateway.image,
            "container_name": name,
            "restart": "unless-stopped",
            "entrypoint": ["python3", "-I", "-B", str(release / ARCHIVE), command,
                           "--config", str(Path(config_path).resolve())],
            "working_dir": str(config.state_dir),
            "volumes": volumes,
            "networks": networks,
            "stop_grace_period": "20s",
            **extra,
        }

    core_network = ipaddress.IPv4Network(container.core_subnet)
    return {
        "name": PROJECT,
        "networks": {
            "lan": {
                "driver": "macvlan",
                "driver_opts": {"parent": container.parent},
                "ipam": {"config": [{"subnet": container.subnet,
                                     "gateway": container.router}]},
            },
            "core": {
                "driver": "bridge",
                "ipam": {"config": [{"subnet": str(core_network),
                                     "gateway": str(core_network.network_address + 1)}]},
            },
        },
        "services": {
            "gateway": gateway,
            "adapter": core(ADAPTER_CONTAINER, "adapter", {
                "lan": {"ipv4_address": container.adapter_ip},
                "core": {"ipv4_address": container.adapter_core},
            }),
            "studio": core(STUDIO_CONTAINER, "setup",
                           {"core": {"ipv4_address": container.studio_core}},
                           ports=[f"{config.setup.port}:{config.setup.port}/tcp"]),
        },
    }


def render(config, config_path, release, output, link=None):
    """Write the Compose project into a new directory; never overwrite.

    ``link`` is the stable path the containers use instead of the verified
    release directory, such as an installation's ``current`` symlink.
    """
    release = _release(release)
    if link is not None:
        release = Path(link)
    files = {COMPOSE: (json.dumps(container_compose(config, config_path, release),
                                  indent=2) + "\n").encode()}
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
    urls = endpoints(config)
    while True:
        status = _get(urls["adapter"] + "/status", {"X-Adapter-Token": token})
        bridge = _get(urls["bridge"] + "/api/config")
        service = status.get("service") if isinstance(status, dict) else None
        result = {
            "adapter": status is not None,
            "adapter_version": service.get("version") if isinstance(service, dict) else None,
            "gateway": bridge is not None,
        }
        if result["adapter"] and result["gateway"]:
            result["ready"] = True
            return result
        if clock() >= deadline:
            result["ready"] = False
            return result
        sleep(0.5)
