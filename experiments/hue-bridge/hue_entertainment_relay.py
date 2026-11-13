"""Host-only, owner-fenced DTLS relay to the selected native Entertainment area."""

import json
import os
import shutil
import ssl
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

from entertainment_stream import NATIVE_AREA_ID, encode_frame, native_colors
from hue_strip_proxy import PLAY_IDS, STRIP_ID

import smart_lighting as lan


class DtlsConnection:
    def __init__(self, config):
        binary = next(
            (
                str(p)
                for p in (
                    Path("/opt/homebrew/opt/openssl/bin/openssl"),
                    Path("/usr/local/opt/openssl/bin/openssl"),
                )
                if p.is_file()
            ),
            shutil.which("openssl"),
        )
        if not binary:
            raise ValueError("OpenSSL is unavailable")
        self.ready = threading.Event()
        self.process = subprocess.Popen(
            [
                binary,
                "s_client",
                "-dtls1_2",
                "-brief",
                "-quiet",
                "-cipher",
                "PSK-AES128-GCM-SHA256",
                "-psk",
                config["client_key"],
                "-psk_identity",
                config.get("application_id", config["hue_key"]),
                "-connect",
                config["bridge"] + ":2100",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

        def drain():
            try:
                for line in self.process.stderr:
                    if b"CONNECTION ESTABLISHED" in line:
                        self.ready.set()
            except (OSError, ValueError):
                pass

        threading.Thread(target=drain, daemon=True, name="hue-dtls-status").start()
        if not self.ready.wait(3) or self.process.poll() is not None:
            self.close()
            raise OSError("Native DTLS handshake failed")
        os.set_blocking(self.process.stdin.fileno(), False)

    def send(self, frame):
        if self.process.poll() is not None:
            raise OSError("Native DTLS process stopped")
        if os.write(self.process.stdin.fileno(), frame) != len(frame):
            raise OSError("Incomplete native frame")

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=1)
        for stream in (self.process.stdin, self.process.stderr):
            if stream:
                stream.close()


class HueEntertainmentRelay:
    def __init__(self, config, requester=None, connection_factory=DtlsConnection):
        if (
            config.get("area_id") != NATIVE_AREA_ID
            or not isinstance(config.get("client_key"), str)
            or len(config["client_key"]) != 32
            or any(x not in "0123456789abcdefABCDEF" for x in config["client_key"])
        ):
            raise ValueError("Invalid selected Entertainment credential")
        self.config = {**config, "bridge": lan.local_ip(config["bridge"])}
        self.requester = requester or self.request
        self.connection_factory = connection_factory
        self.connection = None
        self.owned = False
        self.sequence = 0
        self.ownership_checked = float("-inf")

    def request(self, method, body=None):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=ctx)
        )
        req = urllib.request.Request(
            "https://"
            + self.config["bridge"]
            + "/clip/v2/resource/entertainment_configuration/"
            + NATIVE_AREA_ID,
            method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={
                "hue-application-key": self.config["hue_key"],
                "Content-Type": "application/json",
            },
        )
        with opener.open(req, timeout=1.5) as response:
            result = json.load(response)
        if result.get("errors"):
            raise ValueError("Native Entertainment request rejected")
        return result["data"][0] if method == "GET" else result

    def area(self):
        area = self.requester("GET")
        if area.get("id") != NATIVE_AREA_ID or {
            x.get("rid") for x in area.get("light_services", [])
        } != {STRIP_ID, *PLAY_IDS.values()}:
            raise ValueError("Native area membership changed")
        expected = [
            ("545ed014-5e70-4039-a5de-51a1daa1dc88", 0),
            ("07aeef78-efdf-4c8b-b0f7-0202d20e6e0a", 0),
            ("157ab718-3fd0-4ab2-a5e6-904c5defba6d", 0),
            *[("967b2eb0-0eb2-4099-9807-cdfed3c6383c", i) for i in range(9)],
        ]
        channels = sorted(area.get("channels", []), key=lambda x: x["channel_id"])
        if len(channels) != 12 or any(
            channel["channel_id"] != index
            or len(channel["members"]) != 1
            or (channel["members"][0]["service"]["rid"], channel["members"][0]["index"])
            != expected[index]
            for index, channel in enumerate(channels)
        ):
            raise ValueError("Native channel layout changed")
        return area

    def ours(self, area):
        return area.get("active_streamer", {}).get("rid") == self.config.get(
            "application_id", self.config["hue_key"]
        )

    def start(self):
        area = self.area()
        if area.get("status") == "active":
            if not self.ours(area):
                return False
            self.requester("PUT", {"action": "stop"})
        self.owned = True
        self.ownership_checked = float("-inf")
        try:
            self.requester("PUT", {"action": "start"})
            if not self.ours(self.area()):
                raise ValueError("Native stream ownership not confirmed")
            self.connection = self.connection_factory(self.config)
            return True
        except Exception:
            self.stop()
            raise

    def recover(self):
        self.owned = True
        self.stop()

    def send(self, frame):
        if not self.owned or self.connection is None:
            raise ValueError("No owned native stream")
        if time.monotonic() - self.ownership_checked >= 1:
            area = self.area()
            if area.get("status") != "active" or not self.ours(area):
                raise ValueError("Native stream ownership changed")
            self.ownership_checked = time.monotonic()
        self.connection.send(encode_frame(NATIVE_AREA_ID, native_colors(frame), self.sequence))
        self.sequence = (self.sequence + 1) % 256

    def stop(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None
        if self.owned:
            area = self.area()
            if area.get("status") == "active" and self.ours(area):
                self.requester("PUT", {"action": "stop"})
            self.owned = False
