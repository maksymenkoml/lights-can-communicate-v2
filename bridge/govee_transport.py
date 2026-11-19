"""Single-owner, continuous Govee identity/status receiver for the live adapter."""

import json
import select
import socket
import time

import lights_can_communicate_v2 as lan


class GoveeTransport:
    # A scan proves which device answers at an address. The older H6076 answers
    # only about half of the scans, so a proof is held this long; another
    # device's scan at that address still withdraws the route at once.
    IDENTITY_SECONDS = 5
    # Control needs a native status sample at most this old.
    STATUS_SECONDS = 0.75

    def __init__(self, targets, publisher, remember=None, discovery=None):
        self.targets, self.publisher, self.remember = targets, publisher, remember
        self.discovery = discovery
        self.ids = {lan.device_key(device) for device in targets}
        self.observed, self.native, self.blocked = {}, {}, {}

    def handle(self, message, peer_ip, now, publish=True):
        try:
            ip = lan.local_ip(peer_ip)
            body = message["msg"]
            data = body["data"]
            if not isinstance(data, dict):
                return
            if body["cmd"] == "scan":
                identity = {**data, "ip": ip}
                lan.invalidate_govee_observations([identity])
                try:
                    key = lan.device_key(identity)
                except (ValueError, TypeError, AttributeError):
                    self.blocked[ip] = now
                else:
                    self.observed[(key, ip)] = now
                if self.discovery is not None:
                    try:
                        self.discovery.observe_govee(data, ip, now=now)
                    except Exception:
                        # Optional metadata must not interrupt identity/power proof.
                        pass
            elif body["cmd"] == "devStatus" and data.get("onOff") in (0, 1):
                hints = {device["ip"] for device in self.targets}
                known = {ip for (key, ip) in self.observed if key in self.ids}
                if ip in hints or ip in known:
                    self.native[ip] = (data, now)
                    if data["onOff"] == 0:
                        with lan.GOVEE_ROUTE_LOCK:
                            for key in self.ids:
                                binding = lan.VERIFIED_GOVEE.get(key)
                                if binding is not None and binding[0] == ip:
                                    lan.VERIFIED_GOVEE.pop(key, None)
            if publish:
                self.reconcile(now)
        except (KeyError, TypeError, ValueError):
            return

    def reconcile(self, now):
        self.observed = {
            pair: seen for pair, seen in self.observed.items()
            if 0 <= now - seen <= self.IDENTITY_SECONDS
        }
        self.blocked = {ip: seen for ip, seen in self.blocked.items()
                        if 0 <= now - seen <= self.IDENTITY_SECONDS}
        records = [{"device": key, "ip": ip} for key, ip in self.observed]
        routes = lan.resolve_govee_targets(self.targets, records)
        updates = {}
        changed = False
        with lan.GOVEE_ROUTE_LOCK:
            for device in self.targets:
                key = lan.device_key(device)
                ip = routes.get(key)
                native, seen = self.native.get(ip, (None, float("-inf")))
                valid = (
                    ip is not None
                    and ip not in self.blocked
                    and native is not None
                    and 0 <= now - seen <= self.STATUS_SECONDS
                )
                if not valid:
                    if ip is None or ip in self.blocked or native is None:
                        lan.VERIFIED_GOVEE.pop(key, None)
                        lan.IDENTIFIED_GOVEE.pop(key, None)
                    # Otherwise only the status is late: the bindings keep their
                    # old proof time, which the senders check against their limits.
                    hinted_native, hinted_seen = self.native.get(device["ip"], (None, None))
                    updates[key] = (
                        {
                            "onOff": 0,
                            "identity_verified": False,
                            "lan_diagnostic": {
                                "reason": "identity_missing_or_ambiguous"
                                if ip is None
                                else "identity_blocked"
                                if ip in self.blocked
                                else "native_status_missing",
                                "resolved_ip": ip,
                                "hint_scan_ids": [
                                    found
                                    for (found, address) in self.observed
                                    if address == device["ip"]
                                ],
                                "hint_native_age": now - hinted_seen
                                if hinted_seen is not None
                                else None,
                                "hint_native_on": hinted_native.get("onOff")
                                if hinted_native
                                else None,
                            },
                        },
                        now,
                    )
                    continue
                # The identity is held; the proof is as fresh as the status sample.
                proof_seen = seen
                lan.GOVEE_ROUTES[key] = ip
                lan.IDENTIFIED_GOVEE[key] = (ip, proof_seen)
                if native["onOff"] == 1:
                    lan.VERIFIED_GOVEE[key] = (ip, proof_seen)
                else:
                    lan.VERIFIED_GOVEE.pop(key, None)
                updates[key] = ({**native, "identity_verified": True, "native_sample": seen},
                                proof_seen)
                if device["ip"] != ip:
                    device["ip"] = ip
                    changed = True
        # No route lock is held while taking the controller's state lock.
        self.publisher(updates)
        if changed and self.remember:
            self.remember()

    def receive_loop(self, stop):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("", 4002))
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            sock.setsockopt(
                socket.IPPROTO_IP,
                socket.IP_ADD_MEMBERSHIP,
                socket.inet_aton(lan.MULTICAST) + socket.inet_aton("0.0.0.0"),
            )
            next_poll = next_scan = 0
            while not stop.is_set():
                now = time.monotonic()
                if now >= next_poll:
                    candidates = lan.resolve_govee_targets(
                        self.targets,
                        [
                            {"device": key, "ip": address}
                            for (key, address), seen in self.observed.items()
                            if 0 <= now - seen <= self.IDENTITY_SECONDS
                        ],
                    )
                    for device in self.targets:
                        ip = lan.local_ip(candidates.get(lan.device_key(device), device["ip"]))
                        sock.sendto(
                            lan.udp_packet("scan", {"account_topic": "reserve"}), (ip, 4001)
                        )
                        sock.sendto(lan.udp_packet("devStatus", {}), (ip, 4003))
                    next_poll = now + 0.2
                if now >= next_scan:
                    sock.sendto(
                        lan.udp_packet("scan", {"account_topic": "reserve"}), (lan.MULTICAST, 4001)
                    )
                    next_scan = now + 5
                sock.settimeout(0.05)
                try:
                    raw, peer = sock.recvfrom(8192)
                    self.handle(json.loads(raw), peer[0], time.monotonic(), publish=False)
                    for _ in range(64):
                        if not select.select([sock], [], [], 0)[0]:
                            break
                        raw, peer = sock.recvfrom(8192)
                        self.handle(json.loads(raw), peer[0], time.monotonic(), publish=False)
                except (socket.timeout, ValueError):
                    pass
                self.reconcile(time.monotonic())

    def run(self, stop):
        while not stop.is_set():
            try:
                self.receive_loop(stop)
            except OSError:
                if self.discovery is not None:
                    try:
                        self.discovery.provider_state("govee", "unavailable")
                    except Exception:
                        pass
                with lan.GOVEE_ROUTE_LOCK:
                    for key in self.ids:
                        lan.VERIFIED_GOVEE.pop(key, None)
                        lan.IDENTIFIED_GOVEE.pop(key, None)
                self.publisher(
                    {
                        key: ({"onOff": 0, "identity_verified": False}, time.monotonic())
                        for key in self.ids
                    }
                )
                stop.wait(1)
