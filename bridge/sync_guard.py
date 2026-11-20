"""Restart Sync on the Sync Box when a bridge restart or a stall cost it its frames.

A restart of the bridge (an update, a crash, Docker) while the box streams makes
the box stop Sync, or leaves it reporting Sync on while no frame arrives; while
the bridge warms up the box also drops a new Start. Only a new Start from the
box recovers it, and when Docker Desktop lost the stream port in the restart,
only a further container restart while the box is quiet. The guard does that, a
few times with growing pauses, and never overrides a Stop: Sync stopped while the
bridge runs undisturbed stays off.
"""

import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

# Frames normally arrive within about 11 s of a Start.
STALL_SECONDS = 20.0
# A box that drops Sync this soon after a Start gave up; it did not obey a Stop.
DROP_SECONDS = 30.0
# Pauses before the first, second and third restart, counted from the stall.
RETRY_DELAYS = (0.0, 30.0, 90.0)
RESTART_PAUSE = 3.0
# Sync that stops up to 60 s after a bridge restart was seen, or up to 30 s before
# it is seen (the box stops as the bridge goes down; the restart shows once it is
# back), was stopped by the restart.
RESTART_BEFORE = 60.0
RESTART_AFTER = 30.0
TICK_SECONDS = 5.0
# Stream states that mean no frames: the adapter answers and nobody else streams.
STALLED = ("waiting", "off")


# Docker Desktop can lose a published UDP port when the bridge container restarts:
# `docker port` still lists it, but nothing on the host listens, so the box's
# datagrams are dropped and no Start can help. Only a container restart while
# the box is quiet publishes it again.
STREAM_PORT = 2100
REPAIR_INTERVAL = 60.0
REPAIR_WAIT = 60.0
DOCKER_PATHS = ("/usr/local/bin/docker", "/opt/homebrew/bin/docker",
                "/Applications/Docker.app/Contents/Resources/bin/docker", "/usr/bin/docker")


def udp_port_free(port, host="0.0.0.0"):
    """True when this host can bind the UDP port, i.e. nothing publishes it."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.bind((host, port))
    except OSError:
        return False
    finally:
        probe.close()
    return True


def _docker():
    found = shutil.which("docker")
    if found:
        return found
    return next((path for path in DOCKER_PATHS if Path(path).is_file()), None)


class StreamIngress:
    """The host side of the bridge's stream port: notice it lost and publish it again."""

    def __init__(self, container, bridge_up, port=STREAM_PORT, port_free=udp_port_free,
                 run=subprocess.run, docker=_docker, clock=time.monotonic, sleep=time.sleep):
        self.container = container
        self.bridge_up = bridge_up
        self.port = port
        self.port_free = port_free
        self.run = run
        self.docker = docker
        self.clock = clock
        self.sleep = sleep
        self.repaired_at = None

    def lost(self):
        """The bridge answers, yet nothing on the host receives its stream port."""
        return bool(self.bridge_up()) and self.port_free(self.port)

    def repair(self):
        """Restart the bridge container once per interval; True when the port is back."""
        now = self.clock()
        if self.repaired_at is not None and now - self.repaired_at < REPAIR_INTERVAL:
            return False
        docker = self.docker()
        if docker is None:
            return False
        self.repaired_at = now
        try:
            self.run([docker, "restart", self.container], capture_output=True, timeout=60,
                     check=False)
        except (OSError, subprocess.SubprocessError):
            return False
        end = self.clock() + REPAIR_WAIT
        while self.clock() < end:
            self.sleep(2.0)
            if not self.port_free(self.port) and self.bridge_up():
                return True
        return False


class SyncGuard:
    """Watch the box and the stream; restart Sync when frames do not come."""

    def __init__(self, box, stream_state, bridge_frames=None, ingress=None,
                 clock=time.monotonic, sleep=time.sleep):
        self.box = box
        # StreamIngress, or None where the stream port cannot be checked.
        self.ingress = ingress
        self.stream_state = stream_state
        # Frames the bridge received since it started, or None when it does not answer.
        self.bridge_frames = bridge_frames
        self.frames = None
        self.bridge_restarted_at = None
        self.was_syncing = False
        self.lost_at = None
        self.clock = clock
        self.sleep = sleep
        self.lock = threading.Lock()
        # When Light Studio (the owner or this guard) last started Sync; a box that
        # drops Sync soon after it gave up. Sync started elsewhere is never restarted
        # after the box stops it.
        self.started_at = None
        self.stalled_since = None
        self.attempts = 0
        # True from a restart by the guard until frames flow: Sync is still wanted on.
        self.retrying = False
        self.state = None  # None, "restarting" or "failed"

    # The owner's own Start and Stop from Light Studio.
    def started(self):
        with self.lock:
            self.started_at = self.clock()
            self.stalled_since = None
            self.attempts = 0
            self.retrying = False
            self.state = None

    def stopped(self):
        with self.lock:
            self.was_syncing = False
            self.lost_at = None
            self.started_at = None
            self.stalled_since = None
            self.attempts = 0
            self.retrying = False
            self.state = None

    def public(self):
        """The recovery state for the page: {"recovery": "restarting" | "failed"} or {}."""
        with self.lock:
            return {"recovery": self.state} if self.state else {}

    def _watch_bridge(self, now):
        if self.bridge_frames is None:
            return
        try:
            frames = self.bridge_frames()
        except Exception:  # noqa: BLE001 - an unreadable bridge is a bridge that is down
            frames = None
        if frames is None or (self.frames is not None and frames < self.frames):
            self.bridge_restarted_at = now
        if frames is not None:
            self.frames = frames

    def tick(self):
        """One check; returns True when it restarted Sync."""
        now = self.clock()
        self._watch_bridge(now)
        status = self.box.status()
        if status.get("state") != "ready" or status.get("changing") is not None:
            return False
        syncing = bool(status.get("syncing"))
        with self.lock:
            if self.was_syncing and not syncing:
                self.lost_at = now
            self.was_syncing = syncing
            if self.lost_at is not None and now - self.lost_at > RESTART_AFTER:
                self.lost_at = None
            if (not syncing and self.lost_at is not None and not self.retrying
                    and self.bridge_restarted_at is not None
                    and -RESTART_BEFORE <= self.bridge_restarted_at - self.lost_at
                    <= RESTART_AFTER):
                # The bridge restart stopped Sync: start it again, as if it dropped.
                self.lost_at = None
                self.retrying = True
                self.started_at = now
                self.stalled_since = now - STALL_SECONDS
                self.attempts = 0
        if syncing:
            stream = self.stream_state()
            with self.lock:
                if stream == "active":
                    self.stalled_since = None
                    self.attempts = 0
                    self.retrying = False
                    self.state = None
                    return False
                if stream not in STALLED:
                    # The adapter is down or another app streams: not ours to fix.
                    self.stalled_since = None
                    return False
                if self.stalled_since is None:
                    self.stalled_since = now
                due = self._due(now)
            return self._restart() if due else False
        with self.lock:
            dropped = (self.started_at is not None and self.state != "failed"
                       and (self.retrying or now - self.started_at < DROP_SECONDS))
            if not dropped:
                if self.state != "failed":
                    self.state = None
                self.started_at = None
                self.stalled_since = None
                self.attempts = 0
                self.retrying = False
                return False
            if self.stalled_since is None:
                self.stalled_since = self.started_at
            due = self._due(now)
        return self._restart() if due else False

    def _due(self, now):
        if now - self.stalled_since < STALL_SECONDS:
            return False
        if self.attempts >= len(RETRY_DELAYS):
            self.state = "failed"
            return False
        if now - self.stalled_since < STALL_SECONDS + RETRY_DELAYS[self.attempts]:
            return False
        self.attempts += 1
        self.state = "restarting"
        return True

    def _restart(self):
        self.box.set_sync(False)
        if self.ingress is not None:
            try:
                if self.ingress.lost():
                    # Publish the port again while the box is quiet.
                    self.ingress.repair()
            except Exception:  # noqa: BLE001 - a failed repair still leaves the restart
                pass
        self.sleep(RESTART_PAUSE)
        code, status = self.box.set_sync(True)
        with self.lock:
            # The next stall is counted from this Start.
            self.retrying = True
            self.started_at = self.clock()
            self.stalled_since = None
            if code != 200 or not status.get("syncing"):
                self.stalled_since = self.started_at - STALL_SECONDS
        return True

    def run(self, stop):
        """Check every few seconds until `stop` is set; errors never end the guard."""
        while not stop.wait(TICK_SECONDS):
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - one failed check must not stop the guard
                continue
