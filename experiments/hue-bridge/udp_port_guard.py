"""Keep the opt-in Linux DTLS ingress bound between authenticated sessions."""

import socket
import sys
import threading


def is_client_hello(data):
    """DTLS handshake record (type 22, version 1.x) carrying a ClientHello (type 1)."""
    return (len(data) > 13 and data[0] == 22 and data[1] == 0xFE
            and data[2] in (0xFD, 0xFF) and data[13] == 1)


class UdpPortGuard:
    """Bind before OpenSSL; discard idle packets without forwarding or replaying them.

    Linux delivers packets to the later SO_REUSEADDR OpenSSL listener, and to
    its connected socket after DTLS peer selection. This older socket remains
    bound when that listener exits. Start once before any receiver worker and
    close only after all receiver workers have stopped.
    """

    def __init__(self, host="0.0.0.0", port=2100):
        self.host, self.port = host, port
        self.lock = threading.RLock()
        self.done = threading.Event()
        self.sock = None
        self.worker = None
        self.closed = False
        self.discarded = 0
        self.client_hellos = 0
        self.last_error = None
        self.worker_name = f"dtls-idle-udp-{id(self):x}"

    def start(self):
        """Synchronously bind, failing startup if the ingress cannot be guarded."""
        if sys.platform != "linux":
            raise RuntimeError("UDP port guard requires Linux shared-bind routing")
        with self.lock:
            if self.closed:
                raise ValueError("UDP guard is closed")
            if self.sock is not None:
                if not self.worker.is_alive():
                    raise ValueError("UDP guard worker stopped")
                return
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind((self.host, self.port))
                sock.settimeout(0.1)
            except OSError:
                sock.close()
                raise
            self.sock = sock
            try:
                self.worker = threading.Thread(
                    target=self._drain, name=self.worker_name, daemon=True
                )
                self.worker.start()
            except Exception:
                self.worker = None
                self.sock = None
                sock.close()
                raise

    @property
    def address(self):
        with self.lock:
            if self.sock is None or self.closed or not self.worker.is_alive():
                raise ValueError("UDP guard is not running")
            return self.sock.getsockname()

    @property
    def accept_address(self):
        host, port = self.address
        return f"{host}:{port}"

    def _drain(self):
        while not self.done.is_set():
            try:
                data, _ = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                with self.lock:
                    self.last_error = "socket_error"
                return
            with self.lock:
                self.discarded += 1
                # A peer that OpenSSL is not connected to starts a new DTLS session.
                if is_client_hello(data):
                    self.client_hellos += 1

    def snapshot(self):
        with self.lock:
            return {
                "running": self.worker is not None and self.worker.is_alive(),
                "discarded_datagrams": self.discarded,
                "client_hellos": self.client_hellos,
                "last_error": self.last_error,
            }

    def close(self):
        """Release the port and join the only worker; safe to call repeatedly."""
        with self.lock:
            self.closed = True
            self.done.set()
            worker, sock = self.worker, self.sock
        if worker is not None:
            worker.join()
        if sock is not None:
            sock.close()
