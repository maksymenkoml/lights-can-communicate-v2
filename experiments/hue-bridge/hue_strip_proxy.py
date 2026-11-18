"""Proxy only the selected monitor strip and confirmed Play lights through their bridge."""

import copy
import json
import math
import ssl
import threading
import time
import urllib.request

from bridge_output import strip_changes

import lights_can_communicate_v2 as lan
from lights_can_communicate_v2_core.native_profile import NativeHueProfile

HUE_BRIGHTNESS_SCALES = {"strip": 1.0, "hue_left": 1.0, "hue_center": 0.5, "hue_right": 1.0}


def finite(value, low, high):
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not low <= value <= high
    ):
        raise ValueError("Invalid numeric value")
    return value


def xy_color(value):
    if not isinstance(value, dict):
        raise ValueError("Expected xy color")
    x = finite(value.get("x"), 0, 1)
    y = finite(value.get("y"), 0.00001, 1)
    if x + y > 1.0001:
        raise ValueError("Invalid chromaticity")
    return {"x": x, "y": y}


def hue_payload(state, transitiontime):
    if not isinstance(state, dict) or not isinstance(state.get("on"), bool):
        raise ValueError("Expected boolean on in source state")
    if not state["on"]:
        return None
    duration = round(finite(transitiontime, 0, 1200) * 100)
    changed = strip_changes(state)
    body = {"dynamics": {"duration": duration}}
    if "bri" in changed:
        body["dimming"] = {"brightness": max(0.1, finite(state.get("bri"), 0, 254) / 254 * 100)}
    if "gradient" in changed:
        points = state.get("gradient", {}).get("points", [])
        if not isinstance(points, list) or not 2 <= len(points) <= 5:
            raise ValueError("Expected two to five gradient points")
        body["gradient"] = {
            "points": [
                {"color": {"xy": xy_color(point.get("color", {}).get("xy"))}} for point in points
            ]
        }
        if "dimming" not in body and points[0].get("dimming"):
            body["dimming"] = {
                "brightness": max(0.1, finite(points[0]["dimming"].get("brightness"), 0, 100))
            }
    elif "ct" in changed and state.get("colormode") == "ct":
        body["color_temperature"] = {"mirek": round(finite(state.get("ct"), 153, 500))}
    elif "xy" in changed:
        xy = state.get("xy")
        if not isinstance(xy, list) or len(xy) != 2:
            raise ValueError("Expected xy pair")
        body["color"] = {"xy": xy_color({"x": xy[0], "y": xy[1]})}
    elif ("hue" in changed or "sat" in changed) and state.get("colormode") == "hs":
        # The reference v2 path supplies xy for color scenes; unsupported states fail closed.
        raise ValueError("Hue strip proxy requires xy for colored output")
    if len(body) == 1:
        raise ValueError("No supported color or dimming command")
    return body


class PinnedHueClient:
    def __init__(self, config, requester=None, resource_id=None, profile=None):
        if not isinstance(profile, NativeHueProfile):
            raise ValueError("An explicit native Hue profile is required")
        self.profile = profile
        self.resource_id = profile.strip_id if resource_id is None else resource_id
        if (not isinstance(self.resource_id, str)
                or self.resource_id not in profile.resource_ids):
            raise ValueError("Only the selected profile Hue lights are allowed")
        self.bridge = lan.local_ip(config["bridge"])
        self.key = config["hue_key"]
        self.path = "/clip/v2/resource/light/" + self.resource_id
        self.requester = requester or self.request

    def request(self, method, path, body):
        if path != self.path:
            raise ValueError("Unexpected Hue resource")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context)
        )
        request = urllib.request.Request(
            "https://" + self.bridge + path,
            data=None if body is None else json.dumps(body).encode(),
            method=method,
            headers={"Content-Type": "application/json", "hue-application-key": self.key},
        )
        with opener.open(request, timeout=1.5) as response:
            result = json.load(response)
        if result.get("errors"):
            raise ValueError("Native Hue rejected the selected strip command")
        return result

    def read(self):
        result = self.requester("GET", self.path, None)
        light = result["data"][0]
        if light.get("id") != self.resource_id:
            raise ValueError("Wrong native Hue resource")
        return light

    def write(self, body):
        if (
            not isinstance(body, dict)
            or not body
            or set(body) - {"gradient", "dimming", "color", "color_temperature", "dynamics"}
        ):
            raise ValueError("Unexpected or power-changing Hue write")
        result = self.requester("PUT", self.path, body)
        if result.get("errors") or not any(
            item.get("rid") == self.resource_id for item in result.get("data", [])
        ):
            raise ValueError("Native strip write not confirmed")


    def power(self, on):
        """Switch only this pinned light; the color path never changes power."""
        if type(on) is not bool:
            raise ValueError("Expected boolean power")
        result = self.requester("PUT", self.path, {"on": {"on": on}})
        if result.get("errors") or not any(
            item.get("rid") == self.resource_id for item in result.get("data", [])
        ):
            raise ValueError("Native power write not confirmed")


def merge_hue_body(previous, update):
    result = copy.deepcopy(previous or {})
    colors = {"gradient", "color", "color_temperature"}
    if colors & update.keys():
        for key in colors:
            result.pop(key, None)
    result.update(copy.deepcopy(update))
    return result


class HueStripProxy:
    def __init__(
        self, client, clock=time.monotonic, wall_clock=time.time, role="strip", layout_store=None,
        preview=None, profile=None,
    ):
        client_profile = getattr(client, "profile", None)
        if any(value is not None and not isinstance(value, NativeHueProfile)
               for value in (profile, client_profile)):
            raise ValueError("Invalid native Hue profile")
        if profile is not None and client_profile is not None and profile != client_profile:
            raise ValueError("Conflicting native Hue profiles")
        self.profile = profile if profile is not None else client_profile
        if not isinstance(self.profile, NativeHueProfile):
            raise ValueError("An explicit native Hue profile is required")
        self.layout_store = layout_store
        self.preview = preview
        self.role = role
        resources = self.profile.light_ids
        self.resource_id = getattr(client, "resource_id", resources["strip"])
        if role not in resources or resources[role] != self.resource_id:
            raise ValueError("Hue role does not match the selected resource")
        self.brightness_scale = HUE_BRIGHTNESS_SCALES[role]
        self.applied_scale = self.layout_factor()
        self.client, self.clock, self.wall_clock = client, clock, wall_clock
        self.command_lock = threading.RLock()
        self.state_lock = threading.RLock()
        self.output_lock = threading.RLock()
        self.write_gate = lambda: True
        self.wake = threading.Event()
        self.desired = None
        self.pending_body = None
        self.accepted = None
        self.generation = 0
        self.applied = 0
        self.restore = False
        self.native = None
        self.native_seen = float("-inf")
        self.healthy = False
        self.writes = 0
        self.last_result = "waiting"
        self.last_payload = None

    def layout_factor(self):
        """Return the scene correction times the role's scene fade gain."""
        store = self.layout_store
        if store is None:
            return self.brightness_scale
        scale = getattr(store, "scene_scale", store.scale)(self.role, self.brightness_scale)
        loader = getattr(store, "scene_fade", None)
        fade = loader() if loader is not None else None
        return scale * (fade.get(self.role, 1) if fade else 1)

    def submit(self, payload):
        if (
            not isinstance(payload, dict)
            or set(payload)
            != {"side", "state", "transitiontime", "session", "sequence", "issued_at"}
            or payload["side"] != self.role
        ):
            raise ValueError("Invalid strip command")
        session = payload["session"]
        sequence = payload["sequence"]
        if (
            not isinstance(session, str)
            or not 1 <= len(session) <= 80
            or type(sequence) is not int
            or not 0 <= sequence < 2**63
        ):
            raise ValueError("Invalid source ordering")
        issued = finite(payload["issued_at"], self.wall_clock() - 5, self.wall_clock() + 5)
        body = hue_payload(payload["state"], payload["transitiontime"])
        if (
            body is not None
            and "dimming" not in body
            and self.layout_store is not None
            and self.layout_store.snapshot() is not None
        ):
            body["dimming"] = {
                "brightness": finite(payload["state"].get("bri", 254), 0, 254) / 254 * 100
            }
        if body is not None and "dimming" in body:
            body["dimming"]["brightness"] = max(
                0.1, body["dimming"]["brightness"] * self.brightness_scale
            )
        with self.command_lock:
            previous = self.accepted
            if previous and (
                (session == previous[0] and sequence <= previous[1])
                or (session != previous[0] and issued <= previous[2])
            ):
                return False
            self.accepted = (session, sequence, issued)
            previous_generation = self.generation
            self.generation += 1
            if body is not None:
                previous_body = self.desired[0] if self.desired else None
                self.desired = (merge_hue_body(previous_body, body), issued)
                self.pending_body = (
                    copy.deepcopy(body)
                    if self.applied == previous_generation
                    else merge_hue_body(self.pending_body, body)
                )
            else:
                self.desired = None
                self.pending_body = None
            if body is None:
                self.last_result = "stopped"
            self.wake.set()
        return True

    def included(self):
        # Scenes keep every present lamp, including those with their own light in Sync.
        present = getattr(self.layout_store, "present", None)
        return present(self.role) if present is not None else True

    def govee_allowed(self):
        if not self.included():
            return True
        with self.state_lock:
            return bool(
                self.healthy
                and self.native
                and self.native.get("mode") == "normal"
                and 0 <= self.clock() - self.native_seen <= 2
            )

    def restore_scene(self):
        with self.command_lock:
            self.restore = True
            self.wake.set()

    def process_once(self):
        try:
            if not self.included():
                with self.command_lock:
                    self.restore = True
                with self.state_lock:
                    self.last_result = "excluded from composition"
                return False
            native = self.client.read()
            if native.get("id") != self.resource_id or native.get("mode") not in (
                "normal",
                "streaming",
            ):
                raise ValueError("Unexpected native strip state")
            with self.state_lock:
                self.native = copy.deepcopy(native)
                self.native_seen = self.clock()
            eligible = native.get("mode") == "normal" and native.get("on", {}).get("on") is True
            with self.command_lock:
                if not eligible:
                    self.restore = True
                    with self.state_lock:
                        self.healthy = True
                        self.last_result = (
                            "native streaming" if native["mode"] == "streaming" else "native off"
                        )
                    return False
                scale = self.layout_factor()
                if scale != self.applied_scale:
                    self.restore = True
                    if self.desired is None:
                        brightness = native.get("dimming", {}).get("brightness", 100)
                        base = (
                            brightness / self.applied_scale * self.brightness_scale
                            if self.applied_scale
                            else 100 * self.brightness_scale
                        )
                        self.desired = (
                            {"dimming": {"brightness": base}, "dynamics": {"duration": 400}},
                            self.wall_clock(),
                        )
                if self.desired is None or (self.applied == self.generation and not self.restore):
                    with self.state_lock:
                        self.healthy = True
                    return False
                desired_body, issued = self.desired
                body = desired_body if self.restore else self.pending_body or desired_body
                body = copy.deepcopy(body)
                if self.layout_store is not None and "dimming" in body:
                    body["dimming"]["brightness"] = max(
                        0.1, min(100, body["dimming"]["brightness"] / self.brightness_scale * scale)
                    )
                points = body.get("gradient", {}).get("points", [])
                if points and len(points) > native.get("gradient", {}).get("points_capable", 0):
                    raise ValueError("Native gradient capability mismatch")
                duration = body["dynamics"]["duration"]
                remaining = round(duration - max(0, self.wall_clock() - issued) * 1000)
                body["dynamics"]["duration"] = (
                    max(0, remaining) if remaining > 0 or duration == 0 else 400
                )
                with self.output_lock:
                    if not self.included() or not self.write_gate():
                        self.restore = True
                        self.healthy = True
                        self.last_result = "scene output paused"
                        return False
                    self.client.write(body)
                    if self.preview is not None:
                        try:
                            self.preview.record_hue(self.role, body)
                        except Exception:
                            pass  # Passive telemetry cannot change successful output.
                self.applied = self.generation
                self.applied_scale = scale
                self.restore = False
                self.pending_body = None
                with self.state_lock:
                    self.writes += 1
                    self.last_result = "applied"
                    self.last_payload = copy.deepcopy(body)
                    self.healthy = True
                return True
        except (OSError, ValueError, KeyError, IndexError, TypeError):
            with self.state_lock:
                self.healthy = False
                self.native_seen = float("-inf")
                self.last_result = "native unavailable or rejected"
            return False

    def set_power(self, on, attempts=5, pause=0.2, sleep=time.sleep):
        """Switch the native light and confirm it by reading it back."""
        with self.command_lock:
            self.client.power(on)
            for attempt in range(attempts):
                native = self.client.read()
                if native.get("id") != self.resource_id:
                    raise ValueError("Wrong native Hue resource")
                # Status reports what the light actually does, confirmed or not.
                with self.state_lock:
                    self.native = copy.deepcopy(native)
                    self.native_seen = self.clock()
                if native.get("on", {}).get("on") is on:
                    if on:
                        # Reapply the composition color once the light is on.
                        self.restore = True
                        self.wake.set()
                    return True
                if attempt + 1 < attempts:
                    sleep(pause)
            return False

    def run(self, stop):
        while not stop.is_set():
            self.wake.clear()
            self.process_once()
            self.wake.wait(0.5)

    def snapshot(self):
        with self.state_lock:
            return {
                "resource_id": self.resource_id,
                "brightness_scale": self.brightness_scale,
                "healthy": self.healthy,
                "native_mode": self.native.get("mode") if self.native else None,
                "native_on": self.native.get("on", {}).get("on") if self.native else None,
                "native_brightness": self.native.get("dimming", {}).get("brightness")
                if self.native
                else None,
                "native_gradient_points": len(self.native.get("gradient", {}).get("points", []))
                if self.native
                else 0,
                "native_age": self.clock() - self.native_seen
                if math.isfinite(self.native_seen)
                else None,
                "writes": self.writes,
                "last_result": self.last_result,
                "last_payload": copy.deepcopy(self.last_payload),
            }
