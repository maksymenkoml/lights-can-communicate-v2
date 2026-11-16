"""Opt-in virtual DTLS receiver; native Hue credentials never enter this container."""

import copy
import json
import os
import select
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid

from entertainment_stream import AREA_ID, GROUP_ID, FrameBuffer, decode_frame
from udp_port_guard import UdpPortGuard  # noqa: F401


class StreamOutbox:
    def __init__(self, url, token, sender=None, clock=time.time):
        self.url, self.token, self.clock = url.rstrip("/"), token, clock
        self.sender = sender or self.post
        self.session = str(uuid.uuid4())
        self.lock = threading.RLock()
        self.pending = None
        self.sequence = 0
        self.forwarded = 0
        self.last_error = None
        self.last_http_status = None

    def submit(self, action, colors=None):
        with self.lock:
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
        req = urllib.request.Request(
            self.url + "/stream",
            method="POST",
            data=json.dumps(message).encode(),
            headers={"Content-Type": "application/json", "X-Adapter-Token": self.token},
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=0.3) as response:
            result = json.load(response)
        if not isinstance(result, dict) or result.get("accepted") is not True:
            raise ValueError("Frame rejected by host")

    def forward_once(self):
        with self.lock:
            message, self.pending = self.pending, None
        if message is None or self.clock() - message["issued_at"] > 0.5:
            return False
        try:
            self.sender(message)
        except (OSError, ValueError) as error:
            self.last_error = type(error).__name__
            self.last_http_status = getattr(error, "code", None)
            if isinstance(error, urllib.error.HTTPError):
                error.close()
            return False
        self.forwarded += 1
        self.last_error = None
        self.last_http_status = None
        return True

    def snapshot(self):
        with self.lock:
            return {
                "sequence": self.sequence,
                "forwarded": self.forwarded,
                "last_error": self.last_error,
                "last_http_status": self.last_http_status,
            }

    def run(self, done):
        while not done.is_set():
            self.forward_once()
            done.wait(0.05)
        self.forward_once()


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


class EntertainmentReceiver:
    def __init__(self, url, token, openssl="openssl", enabled=False, udp_guard=None):
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
        from services.entertainment import _psk_value

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
        outbox = StreamOutbox(self.url, self.token)
        self.current_outbox = outbox
        done = threading.Event()
        process = None
        diagnostic_worker = None
        worker = None
        end_reason = "source stopped"
        try:
            process = subprocess.Popen(
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

            def collect_diagnostics():
                for line in iter(process.stderr.readline, b""):
                    category = dtls_diagnostic(line)
                    if category:
                        with self.lock:
                            counts = self.status["dtls_diagnostics"]
                            counts[category] = counts.get(category, 0) + 1

            diagnostic_worker = threading.Thread(
                target=collect_diagnostics, daemon=True, name="dtls-diagnostics"
            )
            diagnostic_worker.start()
            with self.lock:
                self.status.update(active=True, last_result="waiting for DTLS frames")
            worker = threading.Thread(
                target=outbox.run, args=(done,), daemon=True, name="video-outbox"
            )
            worker.start()
            parser = FrameBuffer()
            last_data = time.monotonic()
            received = False
            while (
                group.stream["active"]
                and self.generation is generation
                and group.stream.get("_proc") is process
                and process.poll() is None
            ):
                idle_for = time.monotonic() - last_data
                if not received and idle_for > 45:
                    end_reason = "source timeout"
                    break
                if received and idle_for > 0.5:
                    with self.lock:
                        self.status["last_result"] = "source idle; connection retained"
                readable, _, _ = select.select([process.stdout], [], [], 0.25)
                if not readable:
                    continue
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    end_reason = "DTLS EOF"
                    break
                packets = parser.feed(chunk)
                with self.lock:
                    self.status["rejected_frames"] = parser.rejected_frames
                for packet in packets:
                    frame = decode_frame(packet)
                    outbox.frame(frame)
                    received = True
                    last_data = time.monotonic()
                    with self.lock:
                        self.status.update(
                            active=True, last_result="receiving", version=packet[9], area_id=AREA_ID
                        )
                        self.status["frames_received"] += 1
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
            if process is not None:
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
