"""Stage, activate and roll back releases as journaled, recoverable transactions.

Layout under ``base``: ``config/core.json``, ``state/``, ``releases/<version>/``,
``current`` (symlink to the active release), ``backups/`` and ``install.json``.
Nothing here starts, stops or restarts services; activation and rollback
refuse to run while a component still answers locally.
"""

import argparse
import hashlib
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

from . import __version__, services
from .config import ConfigError, load_config

JOURNAL = "install.json"
# Digest of the configuration that ``services/`` was rendered from.
FINGERPRINT = "config.sha256"
KEEP_BACKUPS = 5
# Live or regenerated data that a snapshot does not need: logs, request logs,
# stream journals and the bridge's own rotating backups.
_SKIP = ("logs", "backup", "requests.jsonl")
_SKIP_SUFFIXES = ("-stream-health.jsonl", "-stream-health.jsonl.1", "requests.jsonl.1")


class InstallError(ValueError):
    """An installation step refused to run; the installation is unchanged."""


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_manifest(release):
    try:
        manifest = json.loads((Path(release) / "manifest.json").read_text())
    except (OSError, ValueError) as error:
        raise InstallError("Release manifest is unavailable") from error
    if (not isinstance(manifest, dict) or type(manifest.get("version")) is not str
            or not isinstance(manifest.get("files"), dict)
            or not isinstance(manifest.get("compatibility"), dict)):
        raise InstallError("Release manifest is invalid")
    return manifest


def verify(release):
    """Return the manifest when every file matches it and nothing else is present."""
    release = Path(release)
    manifest = read_manifest(release)
    present = {p.relative_to(release).as_posix() for p in release.rglob("*")
               if (p.is_file() or p.is_symlink())
               and p.relative_to(release).parts[0] != "services"}
    present.discard("manifest.json")
    if present != set(manifest["files"]):
        raise InstallError("Release files differ from its manifest")
    for name, expected in manifest["files"].items():
        path = release / name
        if path.is_symlink() or _digest(path) != expected:
            raise InstallError("Release file does not match its manifest")
    return manifest


class Installation:
    def __init__(self, base, python=sys.executable, probe=None, clock=time.time):
        self.base = Path(base).absolute()
        self.config_path = self.base / "config" / "core.json"
        self.releases = self.base / "releases"
        self.backups = self.base / "backups"
        self.current = self.base / "current"
        self.python = python
        self.probe = probe or running
        self.clock = clock

    # Journal ------------------------------------------------------------

    def journal(self):
        try:
            data = json.loads((self.base / JOURNAL).read_text())
        except FileNotFoundError:
            data = None
        except (OSError, ValueError) as error:
            raise InstallError("Installation journal is unreadable") from error
        if data is None:
            return {"version": 1, "current": None, "previous": None,
                    "current_backup": None, "transaction": None, "history": []}
        if not isinstance(data, dict) or data.get("version") != 1:
            raise InstallError("Unsupported installation journal")
        return data

    def _write(self, data):
        path = self.base / JOURNAL
        temporary = path.with_name(JOURNAL + ".tmp")
        with temporary.open("w") as stream:
            stream.write(json.dumps(data, indent=2, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        self._sync_dir(self.base)

    @staticmethod
    def _sync_dir(path):
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _stamp(self, reason):
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(self.clock()))
        name, index = f"{stamp}-{reason}", 1
        while (self.backups / name).exists() or (self.backups / (name + ".partial")).exists():
            index += 1
            name = f"{stamp}-{reason}-{index}"
        return name

    # Configuration and state -------------------------------------------

    def config(self):
        return load_config(self.config_path)

    def _config_version(self):
        try:
            return json.loads(self.config_path.read_text()).get("version")
        except (OSError, ValueError, AttributeError) as error:
            raise InstallError("Service configuration is unavailable") from error

    def _fingerprint(self):
        """Digest of the configuration content, independent of its formatting."""
        try:
            value = json.loads(self.config_path.read_text())
        except (OSError, ValueError) as error:
            raise InstallError("Service configuration is unavailable") from error
        canonical = json.dumps(value, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()

    @staticmethod
    def _rendered_for(release):
        try:
            return (release / "services" / FINGERPRINT).read_text().strip()
        except FileNotFoundError:
            return None

    def _stale(self, release):
        """Whether ``release/services`` was rendered from another configuration.

        Releases rendered before fingerprints existed are not known to be stale.
        """
        recorded = self._rendered_for(release)
        return recorded is not None and recorded != self._fingerprint()

    def _render(self, release, config, output):
        services.render(config, self.config_path, release, output, link=self.current)
        (output / FINGERPRINT).write_text(self._fingerprint() + "\n")

    def _rerender(self, release, config):
        """Replace ``release/services`` with files rendered from the current configuration.

        Only this archive's own version may be rendered here: another version's
        supervisor files come from that version's code, through its ``stage``.
        """
        partial = self.releases / (release.name + ".services.partial")
        old = self.releases / (release.name + ".services-old.partial")
        for path in (partial, old):
            shutil.rmtree(path, ignore_errors=True)
        self._render(release, config, partial)
        target = release / "services"
        if target.exists():
            os.replace(target, old)
        os.replace(partial, target)
        self._sync_dir(release)
        shutil.rmtree(old, ignore_errors=True)

    def _refreshable(self, version):
        """Refuse a stale release that this archive cannot render."""
        stale = self._stale(self.releases / version)
        if stale and version != __version__:
            raise InstallError(
                "Configuration changed after staging; stage this release again with its archive")
        return stale

    def _stopped(self, config):
        answering = self.probe(config)
        if answering:
            raise InstallError("Stop these components first: " + ", ".join(answering))

    @staticmethod
    def _skipped(name):
        return name in _SKIP or name.endswith(_SKIP_SUFFIXES)

    def snapshot(self, state, name):
        """Copy durable state into ``backups/name`` through a ``.partial`` directory."""
        self.backups.mkdir(mode=0o700, exist_ok=True)
        partial = self.backups / (name + ".partial")
        final = self.backups / name
        if final.exists():
            return final
        shutil.rmtree(partial, ignore_errors=True)
        shutil.copytree(state, partial, symlinks=True,
                        ignore=lambda _, names: [n for n in names if self._skipped(n)])
        partial.chmod(0o700)
        os.replace(partial, final)
        self._sync_dir(self.backups)
        return final

    def restore(self, state, name):
        """Replace the durable entries of ``state`` with snapshot ``name``; repeatable."""
        source = self.backups / name
        if not source.is_dir():
            raise InstallError("Activation snapshot is missing")
        for entry in state.iterdir():
            if self._skipped(entry.name):
                continue
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()
        for entry in source.iterdir():
            target = state / entry.name
            if entry.is_dir() and not entry.is_symlink():
                shutil.copytree(entry, target, symlinks=True)
            else:
                shutil.copy2(entry, target, follow_symlinks=False)

    def _point(self, version):
        """Atomically point ``current`` at an installed release."""
        temporary = self.base / "current.tmp"
        if temporary.is_symlink() or temporary.exists():
            temporary.unlink()
        temporary.symlink_to(Path("releases") / version)
        os.replace(temporary, self.current)
        self._sync_dir(self.base)

    def _active(self):
        if not self.current.is_symlink():
            return None
        return Path(os.readlink(self.current)).name

    # Commands -----------------------------------------------------------

    def stage(self, release):
        """Install a verified release next to the active one; never activates it."""
        release = Path(release)
        manifest = verify(release)
        version = manifest["version"]
        if version != __version__:
            raise InstallError("Stage a release with its own archive")
        compatibility = manifest["compatibility"]
        if self._config_version() not in compatibility.get("config_version", []):
            raise InstallError("Release does not read this configuration version")
        minimum = tuple(int(part) for part in str(compatibility.get("python", "")).split("."))
        if sys.version_info[:len(minimum)] < minimum:
            raise InstallError("Python is older than the release requires")
        target = self.releases / version
        if target.exists():
            if read_manifest(target) != manifest:
                raise InstallError("A different build of this version is already installed")
            verify(target)
            recorded = self._rendered_for(target)
            if recorded == self._fingerprint():
                return {"version": version, "staged": False, "rendered": False}
            if self._active() == version:
                if recorded is None:
                    # Rendered before digests existed: whether its files match
                    # this configuration is unknown, and recording a digest
                    # without rendering would claim they do.
                    return {"version": version, "staged": False, "rendered": False}
                # Running services were started from these files; replacing them
                # here would let a later `compose down` miss the old containers.
                raise InstallError(
                    "Configuration changed for the active release; stop the components "
                    "and activate it again")
            # Staging again applies a configuration changed since the first stage.
            self._check(target)
            self._rerender(target, self.config())
            return {"version": version, "staged": False, "rendered": True}
        self.releases.mkdir(mode=0o755, parents=True, exist_ok=True)
        partial = self.releases / (version + ".partial")
        shutil.rmtree(partial, ignore_errors=True)
        try:
            shutil.copytree(release, partial, symlinks=True,
                            ignore=shutil.ignore_patterns("services"))
            verify(partial)
            self._check(partial)
            self._render(partial, self.config(), partial / "services")
            os.replace(partial, target)
        except BaseException:
            shutil.rmtree(partial, ignore_errors=True)
            raise
        self._sync_dir(self.releases)
        return {"version": version, "staged": True}

    def _check(self, release):
        """Run the release's own configuration check against the current configuration."""
        check = subprocess.run(
            [self.python, "-I", "-B", str(Path(release) / services.ARCHIVE), "adapter",
             "--check", "--config", str(self.config_path)],
            capture_output=True, timeout=60)
        if check.returncode != 0:
            raise InstallError("Configuration does not validate with this release")

    def activate(self, version):
        data = self.recover()
        if not (self.releases / version).is_dir():
            raise InstallError("Release is not staged")
        verify(self.releases / version)
        stale = self._refreshable(version)
        config = self.config()
        if data["current"] == version and self._active() == version:
            if not stale:
                return {"active": version, "changed": False, "rendered": False}
            # Applies a configuration change to the active release. Like any
            # transition it needs stopped components; the start commands
            # recreate the services from the new files.
            self._stopped(config)
            self._check(self.releases / version)
            self._rerender(self.releases / version, config)
            return {"active": version, "changed": False, "rendered": True,
                    "start": self.start_commands()}
        self._stopped(config)
        # The configuration may have changed since staging.
        self._check(self.releases / version)
        if stale:
            self._rerender(self.releases / version, config)
        data["transaction"] = {"op": "activate", "from": data["current"], "to": version,
                               "backup": self._stamp("activate"), "restore": None,
                               "step": "snapshot"}
        self._write(data)
        return self._finish(data, config)

    def rollback(self):
        data = self.recover()
        previous = data["previous"]
        if previous is None or not (self.releases / previous).is_dir():
            raise InstallError("No previous release to roll back to")
        verify(self.releases / previous)
        stale = self._refreshable(previous)
        config = self.config()
        self._stopped(config)
        if stale:
            self._check(self.releases / previous)
            self._rerender(self.releases / previous, config)
        newer = read_manifest(self.releases / data["current"])["compatibility"].get("state")
        older = read_manifest(self.releases / previous)["compatibility"].get("state")
        data["transaction"] = {"op": "rollback", "from": data["current"], "to": previous,
                               "backup": self._stamp("rollback"),
                               "restore": data["current_backup"] if newer != older else None,
                               "step": "snapshot"}
        self._write(data)
        return self._finish(data, config)

    def recover(self):
        """Complete an interrupted transaction; every step is repeatable."""
        data = self.journal()
        if data["transaction"] is not None:
            config = self.config()
            self._stopped(config)
            self._finish(data, config)
            data = self.journal()
        return data

    def _finish(self, data, config):
        transaction = data["transaction"]
        if transaction["step"] == "snapshot":
            self.snapshot(config.state_dir, transaction["backup"])
            transaction["step"] = "restore" if transaction["restore"] else "switch"
            self._write(data)
        if transaction["step"] == "restore":
            self.restore(config.state_dir, transaction["restore"])
            transaction["step"] = "switch"
            self._write(data)
        if transaction["step"] == "switch":
            self._point(transaction["to"])
            activating = transaction["op"] == "activate"
            data.update(
                current=transaction["to"],
                previous=transaction["from"] if activating else None,
                # A rollback returns to state that matches the older release;
                # its own snapshot is not an upgrade point for a later rollback.
                current_backup=transaction["backup"] if activating else None,
                transaction=None,
            )
            data["history"] = (data["history"] + [{
                "op": transaction["op"], "from": transaction["from"], "to": transaction["to"],
                "backup": transaction["backup"],
                "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.clock())),
            }])[-20:]
            self._write(data)
            self._prune(data)
        return {"active": data["current"], "changed": True, "backup": transaction["backup"],
                "restored": transaction["restore"], "start": self.start_commands()}

    def _prune(self, data):
        if not self.backups.is_dir():
            return
        keep = {data["current_backup"]}
        complete = sorted((p for p in self.backups.iterdir()
                           if p.is_dir() and not p.name.endswith(".partial")),
                          key=lambda p: p.name)
        keep.update(p.name for p in complete[-KEEP_BACKUPS:])
        for path in complete:
            if path.name not in keep:
                shutil.rmtree(path)

    def start_commands(self):
        archive = shlex.quote(str(self.current / services.ARCHIVE))
        config_file = shlex.quote(str(self.config_path))
        # Docker supervises every component; `ready` runs where Python 3.10+ is.
        compose = shlex.quote(str(self.current / "services" / services.COMPOSE))
        return [
            f"docker compose -f {compose} up -d --force-recreate",
            f"docker exec {services.ADAPTER_CONTAINER} python3 -I -B {archive} ready "
            f"--config {config_file}",
        ]

    def status(self):
        data = self.journal()
        staged = sorted(p.name for p in self.releases.iterdir()
                        if p.is_dir() and not p.name.endswith(".partial")) \
            if self.releases.is_dir() else []
        backups = sorted(p.name for p in self.backups.iterdir()
                         if p.is_dir() and not p.name.endswith(".partial")) \
            if self.backups.is_dir() else []
        return {"current": data["current"], "previous": data["previous"],
                "pointer": self._active(), "staged": staged,
                "transaction": data["transaction"], "backups": backups[-KEEP_BACKUPS:]}


def _answers(host, port):
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def running(config):
    """Names of components that still accept connections from this host."""
    urls = services.endpoints(config)
    answering = []
    if _answers(urls["adapter"].split("//", 1)[1].rsplit(":", 1)[0], config.port):
        answering.append("adapter")
    if _answers(*urls["setup"]):
        answering.append("setup")
    if services._get(urls["bridge"] + "/api/config") is not None:
        answering.append("gateway")
    return answering


COMMANDS = ("stage", "activate", "rollback", "status")


def main(argv):
    parser = argparse.ArgumentParser(description="Install, activate or roll back releases")
    parser.add_argument("command", choices=COMMANDS)
    parser.add_argument("version", nargs="?", help="activate: staged release version")
    parser.add_argument("--base", required=True, help="Installation directory")
    parser.add_argument("--release", help="stage: release directory to install")
    args = parser.parse_args(argv)
    installation = Installation(args.base)
    try:
        if args.command == "stage":
            if not args.release:
                raise InstallError("stage requires --release")
            result = installation.stage(args.release)
        elif args.command == "activate":
            if not args.version:
                raise InstallError("activate requires a version")
            result = installation.activate(args.version)
        elif args.command == "rollback":
            result = installation.rollback()
        else:
            result = installation.status()
    except (InstallError, ConfigError) as error:
        print(str(error), file=sys.stderr)
        return 2
    except (OSError, subprocess.SubprocessError):
        print("Installation step failed; rerun the command to complete or recover it",
              file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0
