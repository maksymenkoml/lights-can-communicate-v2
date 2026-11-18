"""Opt-in virtual DTLS receiver; native Hue credentials never enter this container."""

import copy
import errno
import http.client
import os
import select
import socket
import subprocess
import threading
import time
import urllib.error
import uuid

from entertainment_stream import AREA_ID, GROUP_ID, FrameBuffer, decode_frame
from stream_diagnostics import StreamDiagnostics
from stream_http import StreamHttpSender
from udp_port_guard import UdpPortGuard  # noqa: F401


class StreamOutbox:
    def __init__(self, url, token, sender=None, clock=time.time,
                 monotonic=time.monotonic, diagnostics=None):
        self.url, self.token, self.clock = url.rstrip("/"), token, clock
        self.sender = sender or self.post
        self.session = str(uuid.uuid4())
        self.lock = threading.RLock()
        self.pending = None
        self.sequence = 0
        self.forwarded = 0
        self.last_error = None
        self.last_http_status = None
        self.monotonic = monotonic
        self.diagnostics = diagnostics or StreamDiagnostics()
        self.http_sender = None
        self.failed = self.coalesced = self.expired = self.consecutive_failures = 0
        self.last_send_ms = self.max_send_ms = self.max_success_gap = 0
        self.last_success = None
        self.error_counts = {}
        self.last_error_category = None

    def submit(self, action, colors=None):
        with self.lock:
            if self.pending is not None:
                self.coalesced += 1
            self.sequence += 1
            self.pending = {
                "action": action,
                "session": self.session,
                "sequence": self.sequence,
                "issued_at": self.clock(),
            }
            if colors is not None:
                self.pending["colors"] = [[k, *v] for k, v in sorted(colors.items())]

    def frame(self, colors):
        self.submit("frame", colors)

    def stop(self):
        self.submit("stop")

    def post(self, message):
        if self.http_sender is None:
            self.http_sender = StreamHttpSender(self.url, self.token)
        self.http_sender.post(message)

    def forward_once(self):
        with self.lock:
            message, self.pending = self.pending, None
        if message is None:
            return False
        if self.clock() - message["issued_at"] > 0.5:
            with self.lock:
                self.expired += 1
            return False
        started = self.monotonic()
        try:
            self.sender(message)
        except (OSError, ValueError, http.client.HTTPException) as error:
            reason = getattr(error, 'reason', error)
            code = getattr(reason, 'errno', None)
            category = (
                'timeout' if isinstance(reason, TimeoutError) else
                'dns' if isinstance(reason, socket.gaierror) else
                'unreachable' if code in (errno.ENETUNREACH, errno.EHOSTUNREACH) else
                'refused' if code == errno.ECONNREFUSED else
                'http' if isinstance(error, urllib.error.HTTPError) else
                'rejected' if isinstance(error, ValueError) else
                'protocol' if isinstance(error, http.client.HTTPException) else 'other'
            )
            with self.lock:
                self.last_error = type(error).__name__
                self.last_http_status = getattr(error, "code", None)
                self.last_error_category = category
                self.failed += 1
                self.consecutive_failures += 1
                self.error_counts[category] = self.error_counts.get(category, 0) + 1
                self.last_send_ms = (self.monotonic() - started) * 1000
                self.max_send_ms = max(self.max_send_ms, self.last_send_ms)
                self.diagnostics.record('unavailable', 'transport_error', {
                    'failed': self.failed, 'consecutive_failures': self.consecutive_failures,
                    'duration_ms': self.last_send_ms, 'errno': code,
                    'http_status': self.last_http_status,
                })
            if isinstance(error, urllib.error.HTTPError):
                error.close()
            return False
        with self.lock:
            recovered = self.consecutive_failures > 0
            now = self.monotonic()
            if self.last_success is not None:
                self.max_success_gap = max(self.max_success_gap, now - self.last_success)
            self.last_success = now
            self.last_send_ms = (now - started) * 1000
            self.max_send_ms = max(self.max_send_ms, self.last_send_ms)
            self.forwarded += 1
            self.consecutive_failures = 0
            self.last_error = self.last_http_status = self.last_error_category = None
            self.diagnostics.record(
                'stopped' if message['action'] == 'stop' else 'streaming',
                'explicit_stop' if message['action'] == 'stop' else
                'transport_recovered' if recovered else 'frames_received', {
                    'forwarded': self.forwarded, 'failed': self.failed,
                    'duration_ms': self.last_send_ms,
                })
        return True

    def snapshot(self):
        with self.lock:
            return {
                "sequence": self.sequence,
                "forwarded": self.forwarded,
                "last_error": self.last_error,
                "last_http_status": self.last_http_status,
                "failed": self.failed,
                "consecutive_failures": self.consecutive_failures,
                "coalesced": self.coalesced,
                "expired": self.expired,
                "last_send_ms": self.last_send_ms,
                "max_send_ms": self.max_send_ms,
                "last_success_age": (self.monotonic() - self.last_success
                                     if self.last_success is not None else None),
                "max_success_gap": self.max_success_gap,
                "last_error_category": self.last_error_category,
                "error_counts": dict(self.error_counts),
                "connections": self.http_sender.connections if self.http_sender else 0,
            }

    def run(self, done):
        try:
            while not done.is_set():
                self.forward_once()
                done.wait(0.05)
            self.forward_once()
        finally:
            if self.http_sender is not None:
                self.http_sender.close()


def dtls_diagnostic(line):
    value = line.lower()
    for marker, category in (
        (b"read client hello", "client_hello"),
        (b"read finished", "handshake_finished"),
        (b"bad record mac", "authentication_error"),
        (b"address already in use", "listener_bind_error"),
        (b"no shared cipher", "cipher_error"),
        (b"unexpected message", "protocol_error"),
        (b"error:", "other_ssl_error"),
    ):
        if marker in value:
            return category
    return None


RECONNECT_IDLE = 0.5
CANDIDATE_SECONDS = 10


class EntertainmentReceiver:
    def __init__(self, url, token, openssl="openssl", enabled=False, udp_guard=None,
                 diagnostics=None):
        self.url, self.token, self.openssl, self.enabled = url, token, openssl, enabled
        self.udp_guard = udp_guard
        self.lock = threading.RLock()
        self.listener_condition = threading.Condition(self.lock)
        self.closed = False
        self.serving = False
        self.serving_group = None
        self.generation = None
        self.serving_generation = None
        self.bound_group = None
        self.status = {
            "enabled": enabled,
            "active": False,
            "frames_received": 0,
            "last_result": "waiting",
        }
        self.current_outbox = None
        self.diagnostics = diagnostics or StreamDiagnostics()
        self.last_frame_seen = None

    def bind(self, group):
        """Fence the vendor API's synchronous Start/Stop state transitions."""
        if group.id_v2 != GROUP_ID:
            raise ValueError("Only the selected desk area can bind a receiver")
        if self.bound_group is group:
            return
        if self.bound_group is not None:
            raise ValueError("The receiver is already bound")
        original_update = group.update_attr

        def update(values):
            with self.listener_condition:
                if "active" in values.get("stream", {}):
                    self.generation = object()
                return original_update(values)

        with self.listener_condition:
            self.bound_group = group
            self.generation = object()
            group.update_attr = update

    def snapshot(self):
        with self.lock:
            result = copy.deepcopy(self.status)
            if self.current_outbox is not None:
                result["outbox"] = self.current_outbox.snapshot()
            if self.udp_guard is not None:
                result["udp_guard"] = self.udp_guard.snapshot()
            result['frame_age'] = (time.monotonic() - self.last_frame_seen
                                   if self.last_frame_seen is not None else None)
            result['diagnostics'] = self.diagnostics.snapshot()
            return result

    def shutdown(self, timeout=5):
        """Fence new Starts and stop workers before releasing persistent UDP ingress.

        Call from the application finalizer, never from a receiver worker. A
        timeout leaves the guard bound; retry after the blocked worker exits.
        """
        with self.listener_condition:
            self.closed = True
            self.enabled = False
            self.status["enabled"] = False
            self.generation = object()
            group = self.serving_group or self.bound_group
            if group is not None:
                group.update_attr({"stream": {"active": False, "owner": None}})
            self.listener_condition.notify_all()
            if not self.listener_condition.wait_for(lambda: not self.serving, timeout=timeout):
                raise TimeoutError("DTLS worker still running; UDP guard retained")
        if self.udp_guard is not None:
            self.udp_guard.close()

    def serve(self, group, user):
        if (
            not self.enabled
            or group.id_v2 != GROUP_ID
            or {ref().id_v1 for ref in group.lights if ref()} != set("123456")
        ):
            group.update_attr({"stream": {"active": False}})
            return
        if not user.client_key:
            group.update_attr({"stream": {"active": False}})
            with self.lock:
                self.status["last_result"] = "stream client has no key"
            return
        with self.listener_condition:
            if self.closed:
                return
            if self.bound_group is group:
                generation = self.generation
                if group.stream.get("owner") != user.username:
                    return
                if self.serving and self.serving_generation is generation:
                    return
            else:
                generation = object()
                self.generation = generation
            while self.serving:
                if self.serving_generation is generation:
                    return
                self.listener_condition.wait(0.1)
                if self.generation is not generation:
                    return
            if not group.stream.get("active"):
                return
            self.serving = True
            self.serving_group = group
            self.serving_generation = generation
        self._session(group, user, generation)

    def _last_hello(self):
        if self.udp_guard is None:
            return None
        return self.udp_guard.snapshot()["last_client_hello"]

    def _listen(self, user):
        from services.entertainment import _psk_value

        return subprocess.Popen(
            [
                self.openssl,
                "s_server",
                "-4",
                "-dtls1_2",
                "-listen",
                "-quiet",
                "-state",
                "-cipher",
                "PSK-AES128-GCM-SHA256:PSK-AES128-CCM8:PSK-AES128-CCM:@SECLEVEL=0",
                "-psk",
                _psk_value(user.client_key),
                "-psk_identity",
                user.username,
                "-nocert",
                "-accept",
                self.udp_guard.accept_address if self.udp_guard is not None else "2100",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def _collect(self, process):
        worker = threading.Thread(target=self._diagnostics_from, args=(process,),
                                  daemon=True, name="dtls-diagnostics")
        worker.start()
        return worker

    def _diagnostics_from(self, process):
        for line in iter(process.stderr.readline, b""):
            category = dtls_diagnostic(line)
            if category:
                with self.lock:
                    counts = self.status.setdefault("dtls_diagnostics", {})
                    counts[category] = counts.get(category, 0) + 1

    @staticmethod
    def _stop(process, diagnostic_worker=None):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
        if diagnostic_worker is not None:
            diagnostic_worker.join(timeout=1)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream:
                stream.close()

    def _accept(self, outbox, packet, frame):
        outbox.frame(frame)
        now = time.monotonic()
        with self.lock:
            self.status.update(
                active=True, last_result="receiving", version=packet[9], area_id=AREA_ID
            )
            self.status["frames_received"] += 1
            self.last_frame_seen = now
        return now

    def _session(self, group, user, generation):
        outbox = StreamOutbox(self.url, self.token, diagnostics=self.diagnostics)
        self.current_outbox = outbox
        done = threading.Event()
        process = None
        diagnostic_worker = None
        worker = None
        # A replacement listener for a source that re-handshakes from a new
        # port; it only takes over after an authenticated, valid frame.
        candidate = None
        end_reason = "source stopped"
        try:
            process = self._listen(user)
            with self.listener_condition:
                if (
                    self.generation is not generation
                    or not group.stream.get("active")
                    or (self.bound_group is group and group.stream.get("owner") != user.username)
                ):
                    end_reason = "source start superseded"
                    return
                group.stream["_proc"] = process
                group.stream["owner"] = user.username
                self.status["dtls_diagnostics"] = {}
            diagnostic_worker = self._collect(process)
            with self.lock:
                self.status.update(active=True, last_result="waiting for DTLS frames")
            worker = threading.Thread(
                target=outbox.run, args=(done,), daemon=True, name="video-outbox"
            )
            worker.start()
            parser = FrameBuffer()
            last_data = time.monotonic()
            answered = last_data
            received = False
            while (
                group.stream["active"]
                and self.generation is generation
                and group.stream.get("_proc") is process
                and process.poll() is None
            ):
                now = time.monotonic()
                idle_for = now - last_data
                if not received and idle_for > 45:
                    end_reason = "source timeout"
                    break
                hello = self._last_hello()
                if (candidate is None and hello is not None and hello > max(last_data, answered)
                        and idle_for > RECONNECT_IDLE):
                    # Only a ClientHello during the current pause counts; the
                    # established association stays until a replacement proves itself.
                    answered = hello
                    try:
                        started = self._listen(user)
                    except OSError:
                        # No replacement; the established association keeps serving.
                        started = None
                    if started is not None:
                        candidate = {"process": started, "parser": FrameBuffer(), "since": now,
                                     "diagnostics": self._collect(started)}
                if candidate is not None and (candidate["process"].poll() is not None
                                              or now - candidate["since"] > CANDIDATE_SECONDS):
                    self._stop(candidate["process"], candidate["diagnostics"])
                    candidate = None
                if received and idle_for > 0.5:
                    with self.lock:
                        self.status["last_result"] = "source idle; connection retained"
                streams = [process.stdout]
                if candidate is not None:
                    streams.append(candidate["process"].stdout)
                readable, _, _ = select.select(streams, [], [], 0.25)
                if candidate is not None and candidate["process"].stdout in readable:
                    chunk = os.read(candidate["process"].stdout.fileno(), 65536)
                    accepted = []
                    for packet in candidate["parser"].feed(chunk) if chunk else ():
                        try:
                            accepted.append((packet, decode_frame(packet)))
                        except ValueError:
                            continue
                    if not chunk:
                        self._stop(candidate["process"], candidate["diagnostics"])
                        candidate = None
                    elif accepted:
                        with self.listener_condition:
                            if (group.stream.get("_proc") is not process
                                    or self.generation is not generation):
                                break
                            group.stream["_proc"] = candidate["process"]
                            self.status["reconnects"] = self.status.get("reconnects", 0) + 1
                        self._stop(process, diagnostic_worker)
                        process, diagnostic_worker = candidate["process"], candidate["diagnostics"]
                        parser, candidate = candidate["parser"], None
                        self.diagnostics.record("waiting", "source_reconnected", {
                            "client_hellos": self.udp_guard.snapshot()["client_hellos"]})
                        for packet, frame in accepted:
                            last_data = self._accept(outbox, packet, frame)
                        received = True
                        continue
                if process.stdout not in readable:
                    continue
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    end_reason = "DTLS EOF"
                    break
                packets = parser.feed(chunk)
                with self.lock:
                    self.status["rejected_frames"] = parser.rejected_frames
                for packet in packets:
                    last_data = self._accept(outbox, packet, decode_frame(packet))
                    received = True
                if packets and candidate is not None:
                    # The established source resumed; the attempt was not its.
                    self._stop(candidate["process"], candidate["diagnostics"])
                    candidate = None
        except (OSError, ValueError, KeyError, IndexError, TypeError) as error:
            known = {
                "Invalid HueStream header",
                "Unexpected area or frame size",
                "Unexpected v1 frame size",
                "Unexpected v1 light",
                "Unsupported HueStream version",
                "Unexpected or duplicate channel",
                "Invalid xyY stream color",
                "Expected fourteen desk channels",
                "Invalid channel color",
            }
            end_reason = (
                str(error)
                if isinstance(error, ValueError) and str(error) in known
                else type(error).__name__
            )
            with self.lock:
                self.status["last_result"] = "DTLS or frame reception ended"
        finally:
            outbox.stop()
            done.set()
            if candidate is not None:
                self._stop(candidate["process"], candidate["diagnostics"])
            if process is not None:
                self._stop(process, diagnostic_worker)
            with self.listener_condition:
                if group.stream.get("_proc") is process:
                    group.stream.pop("_proc", None)
                    if self.generation is generation:
                        group.update_attr({"stream": {"active": False, "owner": None}})
            if worker is not None:
                worker.join(timeout=0.5)
            with self.listener_condition:
                self.status.update(
                    active=False, forwarded=outbox.forwarded, last_error=outbox.last_error
                )
                self.status["last_result"] = end_reason
                self.serving = False
                self.serving_group = None
                self.listener_condition.notify_all()
            if end_reason == "source timeout":
                self.diagnostics.record("idle", "source_timeout")
            elif end_reason == "DTLS EOF":
                self.diagnostics.record("unavailable", "dtls_closed")
