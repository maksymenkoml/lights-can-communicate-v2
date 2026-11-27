"""The container entrypoint: prepare the data folder, then supervise the core.

``python3 -I -B /app/lights-can-communicate-v2.pyz container`` reads CORE_ADDRESS, TZ
and STUDIO_HOSTS from the environment, creates or converts ``/data/core.json``,
snapshots the durable state when the image version changed, and runs the gateway,
the adapter and Light Studio as child processes that it restarts when they exit.
"""

import ipaddress
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from . import __version__
from .config import (
    CORE_ADDRESS,
    DEFAULT_TIMEZONE,
    VERSION,
    ConfigError,
    _address,
    load_config,
    read_json,
    studio_host_names,
    timezone_name,
)
from .light_registry import REGISTRY_FILE, LightRegistry
from .services import ARCHIVE

DATA = "/data"
# The gateway modules and the core archive (the gateway imports the core package from
# it); the image's own Hue emulator is at /opt/hue-emulator.
PROBE_DIR = Path("/probe")
ARCHIVE_PATH = PROBE_DIR / ARCHIVE
GATEWAY_DIR = Path("/opt/hue-emulator")
PYTHON = "python3"

CONFIG_FILE = "core.json"
# The core network of a version-1 project when it named none.
V1_CORE_SUBNET = "172.30.250.0/29"
# The names of the adapter on a version-1 project's Compose network.
V1_ADAPTER_NAMES = frozenset({"adapter", "lights-can-communicate-v2-adapter"})
VERSION_FILE = "version"
BACKUPS = "backups"
STATE = "state"
KEEP_BACKUPS = 5
# requests.jsonl and the other append-only logs grow without bound (13 MB after a
# few weeks) and can be rebuilt by nothing; they are history, not state. A backup
# skips such a file above this size so that five backups stay small.
LOG_LIMIT = 1_000_000

FIRST_ADAPTER_PORT = 19880
FIRST_SETUP_PORT = 19881

SHUTDOWN_GRACE = 15.0
RESTART_MIN = 1.0
RESTART_MAX = 30.0
RESTART_RESET = 60.0
POLL = 0.2


@dataclass(frozen=True)
class Environment:
    core_address: str
    timezone: str
    studio_hosts: tuple


def parse_environment(env):
    """Read CORE_ADDRESS, TZ and STUDIO_HOSTS; every error is safe to print."""
    raw = env.get("CORE_ADDRESS", "").strip()
    if not raw:
        raise ConfigError("CORE_ADDRESS is required: the free LAN address of the core")
    address = _address(raw, "CORE_ADDRESS")
    if ipaddress.IPv4Address(address).is_unspecified:
        raise ConfigError("CORE_ADDRESS must be a specific LAN address")
    zone = timezone_name(env.get("TZ", "").strip() or DEFAULT_TIMEZONE)
    names = [item.strip() for item in env.get("STUDIO_HOSTS", "").split(",") if item.strip()]
    if not names:
        # Light Studio listens on the core network only and is reached through the
        # NAS's published port, so the browser names the NAS.
        raise ConfigError("STUDIO_HOSTS is required: the NAS address or name that opens "
                          "Light Studio")
    return Environment(address, zone, tuple(studio_host_names(names)))


def _log(message):
    print(f"container: {message}", file=sys.stderr, flush=True)


def _sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_file(path, data, mode=0o600):
    """Replace ``path`` atomically with ``data`` (bytes) and the given mode."""
    path = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".tmp-", delete=False) as stream:
            temporary = stream.name
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        temporary = None
        _sync_directory(path.parent)
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def write_config(path, data):
    write_file(path, (json.dumps(data, indent=2) + "\n").encode())


def _new_token(path):
    """Create a private credential file unless it exists; never replace a token."""
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.exists():
        return
    # Written and synced under a temporary name, then linked into place, so an
    # interrupted start never leaves an empty token that a later start would keep.
    descriptor, partial = tempfile.mkstemp(dir=path.parent, prefix=".token-")
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(secrets.token_hex(32) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(partial, 0o600)
        try:
            os.link(partial, path)
        except FileExistsError:
            return
        _sync_directory(path.parent)
    finally:
        os.unlink(partial)


def new_mac():
    """A random unicast, locally administered MAC address: the bridge identity."""
    octets = bytearray(secrets.token_bytes(6))
    octets[0] = (octets[0] & 0xFC) | 0x02
    return ":".join(f"{octet:02x}" for octet in octets)


def first_start(data, env):
    """Create the tokens, the bridge identity, an empty registry and ``core.json``.

    The gateway flags give a bridge with no lights and no Hue bridge: the
    virtual lights 3-6 and Desk Entertainment belong to the migrated Hue lights
    (see bridge/bootstrap.py), so a new install keeps hue_strip, hue_play and
    entertainment off. Live output and gradients only act on enrolled lights.
    ``core.json`` is written last: a start that stopped earlier repeats safely.
    """
    state = data / STATE
    state.mkdir(mode=0o700, exist_ok=True)
    state.chmod(0o700)
    for name in ("adapter-token", "power-owner-token"):
        # The gateway reads the adapter token from the state directory it serves.
        _new_token(state / name)
    registry = state / REGISTRY_FILE
    if not registry.exists():
        # Without a registry the gateway keeps the two migrated Govee lamps.
        LightRegistry.from_dict({"version": 1, "lights": []}).save(registry)
    write_config(data / CONFIG_FILE, {
        "version": VERSION,
        "state_dir": STATE,
        "adapter": {
            "token_file": f"{STATE}/adapter-token",
            "power_owner_token_file": f"{STATE}/power-owner-token",
            "port": FIRST_ADAPTER_PORT,
            "gradient": True,
            "power_authority": "owner",
        },
        "setup": {"port": FIRST_SETUP_PORT},
        "gateway": {
            "lan_ip": env.core_address,
            "mac": new_mac(),
            "live_output": True,
            "hue_strip": False,
            "hue_play": False,
            "entertainment": False,
            "gradient": True,
            "timezone": env.timezone,
        },
        "container": {"core_address": CORE_ADDRESS, "studio_hosts": list(env.studio_hosts)},
    })


def _migrated_adapter_url(url, subnet):
    """``url`` with the fixed core address when it named the old adapter, else unchanged.

    The old adapter was reached by an address on the old core network (its
    ``core_subnet``, or the version-1 default) or by its Compose service or
    container name; the port stays.
    """
    try:
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port
        if host is None:
            return url
        # A trailing dot is the same DNS name.
        if host.removesuffix(".") not in V1_ADAPTER_NAMES:
            network = ipaddress.IPv4Network(
                subnet if isinstance(subnet, str) else V1_CORE_SUBNET)
            if ipaddress.IPv4Address(host) not in network:
                return url
    except ValueError:
        return url
    netloc = CORE_ADDRESS if port is None else f"{CORE_ADDRESS}:{port}"
    return urlunsplit(parts._replace(netloc=netloc))


def convert_v1(data):
    """Return the version-two form of a version-one configuration.

    Every value stays, including the bridge MAC and the paths of the tokens. The
    ``lan`` block, the core subnet, the Light Studio bind and the gateway's
    container name and image belonged to the Compose project and are dropped. The
    core address is the fixed one of the project file, whatever subnet the old
    project used.
    """
    container = data.get("container")
    if not isinstance(container, dict):
        raise ConfigError("Invalid fields in container")
    result = {key: value for key, value in data.items() if key != "container"}
    result["version"] = VERSION
    gateway = result.get("gateway")
    if isinstance(gateway, dict):
        gateway = {key: value for key, value in gateway.items()
                   if key not in ("container_name", "image")}
        url = gateway.get("adapter_url")
        if isinstance(url, str):
            # The adapter now binds to the fixed core address.
            gateway["adapter_url"] = _migrated_adapter_url(url, container.get("core_subnet"))
        result["gateway"] = gateway
    for section in ("adapter", "setup"):
        value = result.get(section)
        if isinstance(value, dict):
            # The adapter and Light Studio bind to the fixed core address.
            result[section] = {key: item for key, item in value.items() if key != "bind"}
    result["container"] = {"core_address": CORE_ADDRESS,
                           "studio_hosts": container.get("studio_hosts", [])}
    return result


def apply_environment(data, env):
    """Write the values that only the project file may change; return whether it did."""
    path = data / CONFIG_FILE
    original = read_json(path, "service configuration")
    wanted = original
    if original.get("version") == 1:
        wanted = convert_v1(original)
        _log("converted core.json from version 1 to version 2")
    gateway, section = wanted.get("gateway"), wanted.get("container", {})
    if not isinstance(gateway, dict):
        raise ConfigError("Invalid fields in gateway")
    if not isinstance(section, dict):
        raise ConfigError("Invalid fields in container")
    wanted = {**wanted,
              "gateway": {**gateway, "lan_ip": env.core_address, "timezone": env.timezone},
              "container": {**section, "core_address": section.get("core_address", CORE_ADDRESS),
                            "studio_hosts": list(env.studio_hosts)}}
    if wanted != original:
        write_config(path, wanted)
        return True
    return False


def _safe_version(text):
    text = text.strip()
    return text if text and len(text) <= 64 and all(
        c.isalnum() or c in "._+-" for c in text) else "unknown"


def _skip_large_logs(directory, names):
    return [name for name in names
            if name.endswith(".jsonl") and (Path(directory) / name).is_file()
            and (Path(directory) / name).stat().st_size > LOG_LIMIT]


def _read_backup_config(data):
    """``core.json`` as a mapping for a backup; an unreadable file gives defaults."""
    try:
        value = json.loads((data / CONFIG_FILE).read_bytes())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _state_dir(root, config):
    """The state folder that ``config`` names, inside the resolved data folder ``root``
    and outside its backups."""
    value = config.get("state_dir", STATE)
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ConfigError("Invalid path for state_dir")
    state = (root / value).resolve()
    if not state.is_relative_to(root) or state == root:
        # A backup copies the whole state; a folder outside /data is not in the volume.
        raise ConfigError("state_dir must be a folder inside the data folder")
    if state.is_relative_to(root / BACKUPS):
        # A backup is written there: copying it into itself would never end.
        raise ConfigError("state_dir must not be in the backups folder")
    return state


def _require_core_address(address):
    """Stop with a clear message when the project does not give the core its address."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.bind((address, 0))
    except OSError:
        raise ConfigError(f"The core network address {address} is not assigned to this "
                          "container: give the core network this ipv4_address in the "
                          "project file") from None
    finally:
        probe.close()


def _token_paths(root, options, state):
    """Token files named by the adapter section, inside the resolved data folder
    ``root`` and outside the state folder."""
    found = []
    for key in ("token_file", "power_owner_token_file"):
        value = options.get(key) if isinstance(options, dict) else None
        if not isinstance(value, str) or not value or "\x00" in value:
            continue
        path = (root / value).resolve()
        if path.is_file() and path.is_relative_to(root) and not path.is_relative_to(state):
            found.append(path)
    return found


def backup(data, previous, current, now=None):
    """Copy the configuration, tokens and state into ``backups/<stamp>-<from>-<to>``.

    The copy is built in a hidden folder and renamed, so a backup that exists is
    complete. Returns its path.
    """
    config = _read_backup_config(data)
    resolved = data.resolve()
    # Validated before anything is written, so a refused layout leaves no trace.
    state = _state_dir(resolved, config)
    tokens = _token_paths(resolved, config.get("adapter"), state)
    root = data / BACKUPS
    root.mkdir(mode=0o700, exist_ok=True)
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    name = f"{stamp}-{_safe_version(previous)}-{_safe_version(current)}"
    target, counter = root / name, 1
    while target.exists():
        target = root / f"{name}.{counter}"
        counter += 1
    partial = Path(tempfile.mkdtemp(dir=root, prefix=".partial-"))
    try:
        partial.chmod(0o700)
        shutil.copy2(data / CONFIG_FILE, partial / CONFIG_FILE)
        for token in tokens:
            destination = partial / token.relative_to(resolved)
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            shutil.copy2(token, destination)
        if state.is_dir():
            shutil.copytree(state, partial / state.relative_to(resolved),
                            ignore=_skip_large_logs)
        os.replace(partial, target)
    except BaseException:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    return target


def prune_backups(data, keep=KEEP_BACKUPS):
    root = data / BACKUPS
    if not root.is_dir():
        return
    for stale in root.glob(".partial-*"):
        shutil.rmtree(stale, ignore_errors=True)
    folders = sorted(path for path in root.iterdir()
                     if path.is_dir() and not path.name.startswith("."))
    for old in folders[:max(0, len(folders) - keep)]:
        shutil.rmtree(old, ignore_errors=True)


def version_change(data, current=None, now=None):
    """Back up before a new image version starts; record the version afterwards.

    Returns the backup folder, or None when the version did not change or there
    was nothing to keep.
    """
    current = current or __version__
    recorded = None
    try:
        recorded = (data / VERSION_FILE).read_text()
    except OSError:
        pass
    result = None
    if recorded is None or _safe_version(recorded) != current:
        if (data / CONFIG_FILE).exists():
            result = backup(data, "unknown" if recorded is None else recorded, current, now)
            _log(f"backed up the previous state to {result.name}")
        prune_backups(data)
        write_file(data / VERSION_FILE, (current + "\n").encode(), 0o644)
    return result


@dataclass(frozen=True)
class ChildSpec:
    name: str
    argv: tuple
    env: dict
    cwd: str = None


class _Child:
    def __init__(self, spec, delay):
        self.spec = spec
        self.process = None
        self.started = 0.0
        self.delay = delay
        self.restart_at = 0.0


class Supervisor:
    """Run the child processes and restart each one that exits."""

    def __init__(self, specs, *, popen=subprocess.Popen, clock=time.monotonic,
                 sleep=time.sleep, grace=SHUTDOWN_GRACE, minimum=RESTART_MIN,
                 maximum=RESTART_MAX, reset=RESTART_RESET, poll=POLL, log=_log):
        self.children = [_Child(spec, minimum) for spec in specs]
        self.popen, self.clock, self.sleep = popen, clock, sleep
        self.grace, self.minimum, self.maximum = grace, minimum, maximum
        self.reset, self.poll, self.log = reset, poll, log
        self.stopping = False

    def stop(self, *_):
        self.stopping = True

    def _start(self, child):
        spec = child.spec
        try:
            child.process = self.popen(list(spec.argv), env=spec.env, cwd=spec.cwd)
        except OSError:
            child.process = None
            self.log(f"{spec.name} could not start")
            self._schedule(child, ran=0.0)
            return
        child.started = self.clock()
        self.log(f"started {spec.name}")

    def _schedule(self, child, ran):
        if ran >= self.reset:
            child.delay = self.minimum
        child.restart_at = self.clock() + child.delay
        child.delay = min(child.delay * 2, self.maximum)

    def tick(self):
        """Start a due child; note a child that exited. One pass, never blocks."""
        now = self.clock()
        for child in self.children:
            if child.process is None:
                if now >= child.restart_at:
                    self._start(child)
                continue
            code = child.process.poll()
            if code is not None:
                self.log(f"{child.spec.name} exited with {code}")
                child.process = None
                self._schedule(child, ran=now - child.started)

    def shutdown(self):
        alive = [child for child in self.children if child.process is not None]
        for child in alive:
            if child.process.poll() is None:
                child.process.terminate()
        deadline = self.clock() + self.grace
        while self.clock() < deadline and any(c.process.poll() is None for c in alive):
            self.sleep(min(self.poll, 0.1))
        for child in alive:
            if child.process.poll() is None:
                self.log(f"{child.spec.name} did not stop; killing it")
                child.process.kill()
            try:
                child.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            child.process = None

    def run(self, handle_signals=True):
        previous = {}
        if handle_signals:
            for number in (signal.SIGTERM, signal.SIGINT):
                previous[number] = signal.signal(number, self.stop)
        try:
            while not self.stopping:
                self.tick()
                self.sleep(self.poll)
            self.log("stopping")
            self.shutdown()
        finally:
            for number, handler in previous.items():
                signal.signal(number, handler)
        return 0


def children(config, config_path, archive=None, probe=None, gateway_dir=None):
    """The three child processes of a validated configuration."""
    archive = str(ARCHIVE_PATH if archive is None else archive)
    probe = PROBE_DIR if probe is None else Path(probe)
    gateway_dir = GATEWAY_DIR if gateway_dir is None else Path(gateway_dir)
    config_path = str(config_path)
    gateway_env = {**os.environ, **config.gateway.environment(config_path=config.state_dir)}
    core = (PYTHON, "-I", "-B", "-u", archive)
    state = str(config.state_dir)
    return [
        # diyHue reads CONFIG_PATH: the image has no mount at its default directory.
        ChildSpec("gateway", (PYTHON, "-u", str(probe / "bootstrap.py"), "--docker"),
                  gateway_env, str(gateway_dir) if gateway_dir.is_dir() else None),
        ChildSpec("adapter", (*core, "adapter", "--config", config_path), dict(os.environ),
                  state),
        ChildSpec("studio", (*core, "setup", "--config", config_path), dict(os.environ), state),
    ]


def prepare(data, environ):
    """Make the data folder ready for this image and return the validated configuration."""
    data = Path(data)
    env = parse_environment(environ)
    data.mkdir(mode=0o700, parents=True, exist_ok=True)
    existing = (data / CONFIG_FILE).exists()
    if existing:
        version_change(data)
        apply_environment(data, env)
    else:
        first_start(data, env)
        _log("created the configuration, the tokens and the bridge identity")
        write_file(data / VERSION_FILE, (__version__ + "\n").encode(), 0o644)
    return load_config(data / CONFIG_FILE)


def run(data=DATA, environ=None, supervisor=Supervisor):
    # The children run in the state folder: they get absolute paths.
    data = Path(data).absolute()
    config = prepare(data, os.environ if environ is None else environ)
    _require_core_address(config.container.core_address)
    return supervisor(children(config, data / CONFIG_FILE)).run()
