"""The verified connection to the paired Hue bridge, and its current address (#60).

Every client that sends the application key connects through one `HueBridgeLink`:
the certificate must chain to the Signify root CA, and its common name must be the
pinned bridge ID before a key is sent. When the bridge stops answering at its
address, or another device answers there, the link looks for the pinned bridge ID
on the LAN (mDNS) and follows it to its new address. Without a pin it never moves.
"""

import http.client
import os
import re
import secrets
import ssl
import tempfile
import threading
import time
from pathlib import Path

import lights_can_communicate_v2 as lan

# The Signify private root CA that signs Hue Bridge certificates ("Using HTTPS" in
# the Hue developer documentation). SHA-256 F0:BD:8E:65:09:E8:2F:77:4D:63:BC:00:9D:
# 53:88:C9:69:FE:3D:CF:7D:6D:54:1D:63:51:B7:2B:89:8D:8A:CF.
SIGNIFY_ROOT_CA = """\
-----BEGIN CERTIFICATE-----
MIICMjCCAdigAwIBAgIUO7FSLbaxikuXAljzVaurLXWmFw4wCgYIKoZIzj0EAwIw
OTELMAkGA1UEBhMCTkwxFDASBgNVBAoMC1BoaWxpcHMgSHVlMRQwEgYDVQQDDAty
b290LWJyaWRnZTAiGA8yMDE3MDEwMTAwMDAwMFoYDzIwMzgwMTE5MDMxNDA3WjA5
MQswCQYDVQQGEwJOTDEUMBIGA1UECgwLUGhpbGlwcyBIdWUxFDASBgNVBAMMC3Jv
b3QtYnJpZGdlMFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEjNw2tx2AplOf9x86
aTdvEcL1FU65QDxziKvBpW9XXSIcibAeQiKxegpq8Exbr9v6LBnYbna2VcaK0G22
jOKkTqOBuTCBtjAPBgNVHRMBAf8EBTADAQH/MA4GA1UdDwEB/wQEAwIBhjAdBgNV
HQ4EFgQUZ2ONTFrDT6o8ItRnKfqWKnHFGmQwdAYDVR0jBG0wa4AUZ2ONTFrDT6o8
ItRnKfqWKnHFGmShPaQ7MDkxCzAJBgNVBAYTAk5MMRQwEgYDVQQKDAtQaGlsaXBz
IEh1ZTEUMBIGA1UEAwwLcm9vdC1icmlkZ2WCFDuxUi22sYpLlwJY81Wrqy11phcO
MAoGCCqGSM49BAMCA0gAMEUCIEBYYEOsa07TH7E5MJnGw557lVkORgit2Rm1h3B2
sFgDAiEA1Fj/C3AN5psFMjo0//mrQebo0eKd3aWRx+pQY08mk48=
-----END CERTIFICATE-----
"""
_BRIDGE_ID = re.compile(r"[0-9a-f]{16}\Z")


class CertificateChanged(OSError):
    """The bridge certificate does not name the pinned bridge."""


def _check_pin(path, bridge_id):
    """Bind the area client to one bridge.

    The certificate chain is verified against the Signify root CA, and its common
    name is the bridge id. The first verified request records that id; every later
    request must present a certificate for the same bridge before the application
    key is sent. After a bridge replacement, delete the pin file.
    """
    path = Path(path)
    try:
        pinned = path.read_text().strip()
    except FileNotFoundError:
        pinned = None
    if pinned is not None:
        if not secrets.compare_digest(pinned, bridge_id):
            raise CertificateChanged("Unexpected Hue bridge")
        return
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=".hue-pin-", suffix=".tmp",
                                     delete=False) as stream:
        stream.write(bridge_id + "\n")
    os.chmod(stream.name, 0o600)
    os.replace(stream.name, path)


def bridge_context():
    """A TLS context that accepts only certificates issued by the Signify root CA.

    Bridge certificates name the bridge id instead of its address, so the host name
    is not checked here; the caller compares the common name with the pinned id.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_verify_locations(cadata=SIGNIFY_ROOT_CA)
    return context


def bridge_id(certificate):
    """Return the bridge id that a verified certificate names, or raise OSError."""
    subject = dict(item[0] for item in (certificate or {}).get("subject", ()) if item)
    name = subject.get("commonName")
    if not isinstance(name, str) or _BRIDGE_ID.match(name.lower()) is None:
        raise OSError("Hue bridge certificate names no bridge")
    return name.lower()



RELOCATE_INTERVAL = 30.0


class HueBridgeLink:
    def __init__(self, address, pin_file=None, find=None, clock=time.monotonic,
                 relocate_interval=RELOCATE_INTERVAL, connector=None):
        self.address = lan.local_ip(address)
        self.pin_file = pin_file
        self.find = find
        self.clock = clock
        self.relocate_interval = relocate_interval
        self.connector = connector or self._open
        self.lock = threading.Lock()
        self.relocated_at = None

    @staticmethod
    def _open(address, timeout):
        """A connected HTTPS connection to `address` and the bridge ID it presents."""
        connection = http.client.HTTPSConnection(address, 443, timeout=timeout,
                                                 context=bridge_context())
        try:
            connection.connect()
            return connection, bridge_id(connection.sock.getpeercert())
        except BaseException:
            connection.close()
            raise

    def _pinned(self):
        if self.pin_file is None:
            return None
        try:
            value = self.pin_file.read_text().strip()
        except OSError:
            return None
        return value or None

    def _verified(self, address, timeout):
        connection, identity = self.connector(address, timeout)
        try:
            if self.pin_file is not None:
                # The first verified connection records the bridge ID.
                _check_pin(self.pin_file, identity)
        except BaseException:
            connection.close()
            raise
        return connection

    def connect(self, timeout=1.5):
        """A connected, verified connection to the pinned bridge; raise OSError."""
        address = self.address
        try:
            return self._verified(address, timeout)
        except (OSError, ssl.SSLError, http.client.HTTPException) as error:
            moved = self.relocate(address, timeout)
            if moved is None:
                if isinstance(error, OSError):
                    raise
                raise OSError("Hue bridge unreachable") from None
            return self._verified(moved, timeout)

    def relocate(self, failed, timeout=1.5):
        """Find the pinned bridge at another LAN address; return it, or None."""
        pinned = self._pinned()
        if pinned is None or self.find is None:
            return None
        with self.lock:
            if self.address != failed:
                return self.address  # another client already followed the bridge
            now = self.clock()
            if (self.relocated_at is not None
                    and now - self.relocated_at < self.relocate_interval):
                return None
            self.relocated_at = now
            try:
                candidates = self.find()
            except OSError:
                return None
            for candidate in candidates:
                try:
                    candidate = lan.local_ip(candidate)
                except ValueError:
                    continue
                if candidate == failed:
                    continue
                try:
                    connection, identity = self.connector(candidate, timeout)
                except (OSError, ssl.SSLError, http.client.HTTPException, ValueError):
                    continue
                connection.close()
                if identity == pinned:
                    self.address = candidate
                    print("Paired Hue bridge moved to a new address.", flush=True)
                    return candidate
        return None
