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
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

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
ARCHIVE_PATH = Path("/app") / ARCHIVE
# The gateway modules of the release; the image's own Hue emulator is at /opt/hue-emulator.
PROBE_DIR = Path("/probe")
GATEWAY_DIR = Path("/opt/hue-emulator")
PYTHON = "python3"

CONFIG_FILE = "core.json"
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
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return
    with os.fdopen(descriptor, "w") as stream:
        stream.write(secrets.token_hex(32) + "\n")


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


def convert_v1(data):
    """Return the version-two form of a version-one configuration.

    Every value stays, including the bridge MAC and the paths of the tokens. The
    ``lan`` block, the core subnet and the gateway's container name and image
    belonged to the Compose project and are dropped. The adapter's core address
    is the one the subnet gave it (its third host).
    """
    container = data.get("container")
    if not isinstance(container, dict):
        raise ConfigError("Invalid fields in container")
    result = {key: value for key, value in data.items() if key != "container"}
    result["version"] = VERSION
    gateway = result.get("gateway")
    if isinstance(gateway, dict):
        result["gateway"] = {key: value for key, value in gateway.items()
                             if key not in ("container_name", "image")}
    address = CORE_ADDRESS
    subnet = container.get("core_subnet")
    if subnet is not None:
        try:
            address = str(ipaddress.IPv4Network(subnet).network_address + 3)
        except (ValueError, TypeError):
            raise ConfigError("Invalid container core subnet") from None
    result["container"] = {"core_address": address,
                           "studio_hosts": container.get("studio_hosts", [])}
    return result


def apply_environment(data, env):
    """Write the values that only the project file may change; return whether it did."""
    path = data / CONFIG_FILE
    config = read_json(path, "service configuration")
    if config.get("version") == 1:
        config = convert_v1(config)
        _log("converted core.json from version 1 to version 2")
    wanted = json.loads(json.dumps(config))
    gateway = wanted.get("gateway")
    if not isinstance(gateway, dict):
        raise ConfigError("Invalid fields in gateway")
    gateway["lan_ip"] = env.core_address
    gateway["timezone"] = env.timezone
    section = wanted.setdefault("container", {})
    if not isinstance(section, dict):
        raise ConfigError("Invalid fields in container")
    section.setdefault("core_address", CORE_ADDRESS)
    section["studio_hosts"] = list(env.studio_hosts)
    if wanted != json.loads(path.read_bytes()):
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


def _token_paths(data):
    """Token files named by the configuration that lie outside ``state/``."""
    try:
        options = json.loads((data / CONFIG_FILE).read_bytes())["adapter"]
    except (OSError, ValueError, KeyError, TypeError):
        return []
    state = (data / STATE).resolve()
    found = []
    for key in ("token_file", "power_owner_token_file"):
        value = options.get(key) if isinstance(options, dict) else None
        if not isinstance(value, str) or not value or "\x00" in value:
            continue
        path = (data / value).resolve()
        if path.is_file() and path.is_relative_to(data.resolve()) and not path.is_relative_to(
                state):
            found.append(path)
    return found


def backup(data, previous, current, now=None):
    """Copy the configuration, tokens and state into ``backups/<stamp>-<from>-<to>``.

    The copy is built in a hidden folder and renamed, so a backup that exists is
    complete. Returns its path.
    """
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
        for token in _token_paths(data):
            destination = partial / token.relative_to(data.resolve())
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            shutil.copy2(token, destination)
        if (data / STATE).is_dir():
            shutil.copytree(data / STATE, partial / STATE, ignore=_skip_large_logs)
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
    def __init__(self, spec):
        self.spec = spec
        self.process = None
        self.started = 0.0
        self.delay = RESTART_MIN
        self.restart_at = 0.0


class Supervisor:
    """Run the child processes and restart each one that exits."""

    def __init__(self, specs, *, popen=subprocess.Popen, clock=time.monotonic,
                 sleep=time.sleep, grace=SHUTDOWN_GRACE, minimum=RESTART_MIN,
                 maximum=RESTART_MAX, reset=RESTART_RESET, poll=POLL, log=_log):
        self.children = [_Child(spec) for spec in specs]
        self.popen, self.clock, self.sleep = popen, clock, sleep
        self.grace, self.minimum, self.maximum = grace, minimum, maximum
        self.reset, self.poll, self.log = reset, poll, log
        self.stopping = False
        for child in self.children:
            child.delay = minimum

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
    config = prepare(data, os.environ if environ is None else environ)
    return supervisor(children(config, Path(data) / CONFIG_FILE)).run()
