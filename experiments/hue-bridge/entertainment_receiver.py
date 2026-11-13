"""Opt-in virtual DTLS receiver; native Hue credentials never enter this container."""

import copy
import json
import os
import select
import subprocess
import threading
import time
import urllib.request
import uuid

from entertainment_stream import AREA_ID, GROUP_ID, FrameBuffer, decode_frame


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
        if result.get("accepted") is not True:
            raise ValueError("Frame rejected by host")

    def forward_once(self):
        with self.lock:
            message, self.pending = self.pending, None
        if message is None or self.clock() - message["issued_at"] > 0.5:
            return False
        try:
            self.sender(message)
        except (OSError, ValueError):
            self.last_error = "host frame delivery unavailable"
            return False
        self.forwarded += 1
        self.last_error = None
        return True

    def run(self, done):
        while not done.is_set():
            self.forward_once()
            done.wait(0.05)
        self.forward_once()


class EntertainmentReceiver:
    def __init__(self, url, token, openssl="openssl", enabled=False):
        self.url, self.token, self.openssl, self.enabled = url, token, openssl, enabled
        self.lock = threading.RLock()
        self.status = {
            "enabled": enabled,
            "active": False,
            "frames_received": 0,
            "last_result": "waiting",
        }

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.status)

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
        outbox = StreamOutbox(self.url, self.token)
        done = threading.Event()
        process = None
        try:
            process = subprocess.Popen(
                [
                    self.openssl,
                    "s_server",
                    "-4",
                    "-dtls1_2",
                    "-listen",
                    "-quiet",
                    "-cipher",
                    "PSK-AES128-GCM-SHA256:PSK-AES128-CCM8:PSK-AES128-CCM:@SECLEVEL=0",
                    "-psk",
                    _psk_value(user.client_key),
                    "-psk_identity",
                    user.username,
                    "-nocert",
                    "-accept",
                    "2100",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            group.stream["_proc"] = process
            group.stream["owner"] = user.username
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
                and group.stream.get("_proc") is process
                and process.poll() is None
            ):
                if time.monotonic() - last_data > (2 if received else 45):
                    break
                readable, _, _ = select.select([process.stdout], [], [], 0.25)
                if not readable:
                    continue
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    break
                for packet in parser.feed(chunk):
                    frame = decode_frame(packet)
                    outbox.frame(frame)
                    received = True
                    last_data = time.monotonic()
                    with self.lock:
                        self.status.update(
                            active=True, last_result="receiving", version=packet[9], area_id=AREA_ID
                        )
                        self.status["frames_received"] += 1
        except (OSError, ValueError, KeyError, IndexError, TypeError):
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
                for stream in (process.stdin, process.stdout):
                    if stream:
                        stream.close()
            if group.stream.get("_proc") is process:
                group.stream.pop("_proc", None)
                group.update_attr({"stream": {"active": False, "owner": None}})
            with self.lock:
                self.status.update(
                    active=False, forwarded=outbox.forwarded, last_error=outbox.last_error
                )
