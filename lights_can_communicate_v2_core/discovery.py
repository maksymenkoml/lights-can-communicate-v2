"""Bounded metadata catalog. Discovery never authorizes a hardware writer."""

import hashlib
import ipaddress
import itertools
import math
import re
import threading
import time
import uuid

_MAC = re.compile(r"(?:[0-9a-fA-F]{2}:){7}[0-9a-fA-F]{2}|[0-9a-fA-F]{16}")
_BRIDGE = re.compile(r"[0-9a-fA-F]{16}")
_ROLE = re.compile(r"[a-z][a-z0-9_-]{0,63}")
_PROFILES = {
    ("govee", "H6076"): ("floor", 7, 136, None),
    # Table Lamp 2: one color through colorwc until its segments are tested live.
    ("govee", "H6022"): ("table", 1, 26, 16),
    ("hue", "440400982841"): ("bar", 1, None, 25),
    ("hue", "440400982842"): ("bar", 1, None, 25),
    ("hue", "LCX005"): ("strip", 9, None, None),
    ("hue", "LCX006"): ("strip", 9, None, None),
}


def _label(value, limit, fallback=""):
    if not isinstance(value, str):
        return fallback
    # Slice before filtering so even malformed metadata has bounded work.
    return "".join(char for char in value[:limit] if char.isprintable()).strip() or fallback


def _mac(value):
    if not isinstance(value, str) or not _MAC.fullmatch(value):
        return None
    compact = value.replace(":", "").upper()
    return ":".join(compact[index:index + 2] for index in range(0, 16, 2))


def _resource(value):
    if not isinstance(value, str) or len(value) != 36:
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None


def _local_ip(value):
    if not isinstance(value, str) or len(value) > 15:
        return None
    try:
        address = ipaddress.IPv4Address(value)
    except ValueError:
        return None
    if not address.is_private or address.is_multicast or address.is_unspecified:
        return None
    return str(address)


def _finite_positive(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value > 0)


class DeviceCatalog:
    """Thread-safe, copied public projections of recent physical observations."""

    def __init__(self, legacy_roles=None, clock=time.monotonic, ttl=20, max_devices=128,
                 rejoin_roles=None):
        if (not _finite_positive(ttl) or type(max_devices) is not int
                or not 1 <= max_devices <= 4096):
            raise ValueError("Invalid discovery bounds")
        self._clock, self._ttl, self._max_devices = clock, ttl, max_devices
        self._lock = threading.RLock()
        self._entries = {}
        self._contradictory_peers = {}
        self._govee_blocked_until = float("-inf")
        self._providers = {"govee": "unavailable", "hue": "unavailable"}
        # An enrolled migrated lamp owns its layout row and is not enrolled again. A
        # removed one (rejoin_roles) names its migrated row but can be enrolled; the
        # registry then gives it its migrated id back.
        self._legacy_roles = self._roles(legacy_roles, max_devices)
        self._rejoin_roles = self._roles(rejoin_roles, max_devices)

    def _roles(self, roles, max_devices):
        result = {}
        if isinstance(roles, dict):
            for key, role in itertools.islice(roles.items(), max_devices):
                if not isinstance(key, tuple) or len(key) != 2:
                    continue
                provider, identity = key
                identity = _mac(identity) if provider == "govee" else _resource(identity)
                if (provider in self._providers and identity and isinstance(role, str)
                        and _ROLE.fullmatch(role)):
                    result[(provider, identity)] = role
        return result

    def _now(self, now=None):
        value = self._clock() if now is None else now
        if (not isinstance(value, (int, float)) or isinstance(value, bool)
                or not math.isfinite(value)):
            raise ValueError("Invalid discovery time")
        return value

    def _fresh(self, seen, now):
        return 0 <= now - seen <= self._ttl

    def _prune(self, now):
        self._contradictory_peers = {
            address: seen for address, seen in self._contradictory_peers.items()
            if self._fresh(seen, now)
        }
        for entry in self._entries.values():
            entry["addresses"] = {
                address: seen for address, seen in entry["addresses"].items()
                if self._fresh(seen, now)
            }

    def _entry(self, provider, identity, physical_id, now):
        identifier = "device-" + hashlib.sha256(
            (provider + ":" + identity).encode("ascii")
        ).hexdigest()[:32]
        if identifier not in self._entries:
            if len(self._entries) >= self._max_devices:
                stale = [key for key, value in self._entries.items()
                         if not self._fresh(value["seen"], now)]
                if not stale:
                    return None
                oldest = min(stale, key=lambda key: (self._entries[key]["seen"], key))
                del self._entries[oldest]
            self._entries[identifier] = {
                "id": identifier, "provider": provider, "identity": identity,
                "physical_id": physical_id, "seen": now, "addresses": {},
                "blocked_until": float("-inf"), "ambiguous": False,
                "name": "", "model": "",
            }
        return self._entries[identifier]

    def observe_govee(self, data, peer_ip, now=None):
        """Observe scan metadata supplied by the adapter's existing UDP listener."""
        if not isinstance(data, dict):
            return
        identity, address = _mac(data.get("device")), _local_ip(peer_ip)
        if address is None:
            return
        now = self._now(now)
        with self._lock:
            self._prune(now)
            if identity is None:
                if (address in self._contradictory_peers
                        or len(self._contradictory_peers) < self._max_devices):
                    self._contradictory_peers[address] = now
                else:
                    # Retain every live marker. Overflow blocks Govee selection globally.
                    self._govee_blocked_until = max(self._govee_blocked_until, now + self._ttl)
                for known in self._entries.values():
                    if known["provider"] == "govee" and address in known["addresses"]:
                        known["blocked_until"] = max(known["blocked_until"], now + self._ttl)
                return
            entry = self._entry("govee", identity, identity, now)
            if entry is None:
                # Even an unadmitted identity can contradict an admitted address.
                for other in self._entries.values():
                    if other["provider"] == "govee" and address in other["addresses"]:
                        other["blocked_until"] = now + self._ttl
                return
            contradiction = self._contradictory_peers.get(address)
            if contradiction is not None:
                entry["blocked_until"] = max(entry["blocked_until"], contradiction + self._ttl)
            for other in self._entries.values():
                if (other is not entry and other["provider"] == "govee"
                        and address in other["addresses"]):
                    other["blocked_until"] = now + self._ttl
                    entry["blocked_until"] = now + self._ttl
            addresses = entry["addresses"]
            if address not in addresses and len(addresses) >= 4:
                entry["blocked_until"] = now + self._ttl
                del addresses[min(addresses, key=addresses.get)]
            addresses[address] = now
            entry["seen"] = now
            entry["model"] = _label(data.get("sku"), 64).upper()
            reported_name = _label(data.get("deviceName"), 80)
            if reported_name:
                entry["reported_name"] = reported_name
            suffix = identity.replace(":", "")[-8:]
            fallback = f"Govee {entry['model'][:48] or 'unknown model'} · {suffix}"
            entry["name"] = entry.get("reported_name") or fallback
            self._providers["govee"] = "ready"

    def observe_hue(self, bridge_id, lights, devices, now=None):
        """Join native light owners to device models without importing output state."""
        if (not isinstance(bridge_id, str) or not _BRIDGE.fullmatch(bridge_id)
                or not isinstance(lights, list) or not isinstance(devices, list)):
            return
        bridge_id, now = bridge_id.upper(), self._now(now)
        limit = self._max_devices * 4
        models, duplicate_owners = {}, set()
        for device in devices[:limit]:
            if not isinstance(device, dict):
                continue
            identity = _resource(device.get("id"))
            if identity is None:
                continue
            if identity in models:
                duplicate_owners.add(identity)
            product = device.get("product_data")
            models[identity] = _label(product.get("model_id"), 64).upper() \
                if isinstance(product, dict) else ""
        with self._lock:
            self._prune(now)
            seen = set()
            for light in lights[:limit]:
                if not isinstance(light, dict):
                    continue
                physical = _resource(light.get("id"))
                if physical is None:
                    continue
                owner = light.get("owner")
                owner_id = _resource(owner.get("rid")) if isinstance(owner, dict) else None
                if owner_id is None or owner.get("rtype", "device") != "device":
                    continue
                entry = self._entry("hue", bridge_id + ":" + physical, physical, now)
                if entry is None:
                    continue
                metadata = light.get("metadata")
                entry["name"] = _label(metadata.get("name"), 80, "Hue light") \
                    if isinstance(metadata, dict) else "Hue light"
                entry["model"] = models.get(owner_id, "")
                entry["seen"] = now
                entry["ambiguous"] = physical in seen or owner_id in duplicate_owners
                seen.add(physical)
            self._providers["hue"] = "ready"

    def provider_state(self, provider, state):
        """Set a bounded provider diagnostic; never expose exception text."""
        with self._lock:
            if provider in self._providers:
                states = {"ready", "discovering", "unavailable", "not_configured", "stale"}
                self._providers[provider] = state if isinstance(state, str) and state in states \
                    else "unavailable"

    def govee_enrollment(self, identifier):
        """Return the device ID, address and model of an enrollable Govee light, or None.

        The binding rules of a layout placement apply at this moment: the light is
        discovered, online, unambiguous and not one of the migrated lamps. Any model
        qualifies; one outside the capability table is driven as a basic light.
        """
        now = self._now()
        with self._lock:
            self._prune(now)
            entry = self._entries.get(identifier)
            if (entry is None or entry["provider"] != "govee"
                    or self._providers["govee"] != "ready" or not self._fresh(entry["seen"], now)
                    or not entry["model"]
                    or (entry["provider"], entry["physical_id"]) in self._legacy_roles
                    or self._govee_blocked_until >= now or entry["blocked_until"] >= now
                    or len(entry["addresses"]) != 1):
                return None
            address = next(iter(entry["addresses"]))
            if any(other is not entry and other["provider"] == "govee"
                   and address in other["addresses"] for other in self._entries.values()):
                return None
            return {"device": entry["physical_id"], "ip": address, "model": entry["model"]}

    def hue_enrollment(self, identifier):
        """Return the native light UUID and model of an enrollable Hue light, or None.

        The light is discovered, online, unambiguous and not one of the migrated
        lamps. Whether it can stream is checked against the paired Entertainment
        area, not against a model list.
        """
        now = self._now()
        with self._lock:
            entry = self._entries.get(identifier)
            if (entry is None or entry["provider"] != "hue"
                    or self._providers["hue"] != "ready" or not self._fresh(entry["seen"], now)
                    or entry["ambiguous"]
                    or (entry["provider"], entry["physical_id"]) in self._legacy_roles):
                return None
            return {"light": entry["physical_id"], "model": entry["model"] or "unknown"}

    def snapshot(self):
        now = self._now()
        with self._lock:
            self._prune(now)
            owners = {}
            for entry in self._entries.values():
                if entry["provider"] == "govee":
                    for address in entry["addresses"]:
                        owners.setdefault(address, set()).add(entry["id"])
            rows = []
            for identifier in sorted(self._entries):
                entry = self._entries[identifier]
                shape, zones, height, width = _PROFILES.get(
                    (entry["provider"], entry["model"]), (None, None, None, None)
                )
                online = (self._providers[entry["provider"]] == "ready"
                          and self._fresh(entry["seen"], now))
                ambiguous = entry["ambiguous"] if entry["provider"] == "hue" else (
                    self._govee_blocked_until >= now or entry["blocked_until"] >= now
                    or len(entry["addresses"]) > 1
                    or any(len(owners[address]) > 1 for address in entry["addresses"])
                )
                rows.append({
                    "id": identifier, "provider": entry["provider"], "name": entry["name"],
                    "model": entry["model"], "shape": shape, "zones": zones,
                    "supported": shape is not None, "online": online,
                    "selectable": online and shape is not None and not ambiguous,
                    "ambiguous": ambiguous,
                    "layout_id": (self._legacy_roles.get((entry["provider"], entry["physical_id"]))
                                  or self._rejoin_roles.get((entry["provider"],
                                                             entry["physical_id"]))),
                    "height_cm": height, "width_cm": width,
                })
            return {"connected": True, "devices": rows, "providers": dict(self._providers)}
