"""Proxy only the native Hue lights of the profile through their bridge."""

import copy
import http.client
import json
import math
import threading
import time

from bridge_output import strip_changes
from entertainment_stream import default_scale

import lights_can_communicate_v2 as lan
from lights_can_communicate_v2_core.native_profile import HueLightSet, NativeHueProfile

# The area's profile, or every Hue light the core drives (scenes-only ones too, #78).
_LIGHT_SETS = (NativeHueProfile, HueLightSet)

# Hue bridges accept at most 32 characters in a light's name. The layout limits
# the Hue lamp names to the same length, so a longer name is refused, not cut.
HUE_NAME_MAX = 32
NAME_RETRY_SECONDS = 30



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


# Errors of a kept-alive connection that the bridge closed while it was idle.
STALE_CONNECTION = (
    http.client.RemoteDisconnected,
    http.client.CannotSendRequest,
    ConnectionResetError,
    BrokenPipeError,
    ConnectionAbortedError,
)


class PinnedHueClient:
    def __init__(self, config, requester=None, resource_id=None, profile=None,
                 connection_factory=None, link=None):
        if not isinstance(profile, _LIGHT_SETS):
            raise ValueError("An explicit native Hue profile is required")
        self.profile = profile
        self.resource_id = resource_id
        if (not isinstance(self.resource_id, str)
                or self.resource_id not in profile.resource_ids):
            raise ValueError("Only the selected profile Hue lights are allowed")
        self.bridge = lan.local_ip(config["bridge"])
        self.key = config["hue_key"]
        self.path = "/clip/v2/resource/light/" + self.resource_id
        self.requester = requester or self.request
        # The verified connection to the pinned bridge (HueBridgeLink).
        self.link = link
        self.connection_factory = connection_factory or self.connect
        self.connection = None
        # Status polling and power writes run on different threads.
        self.connection_lock = threading.Lock()

    def connect(self):
        # The key is sent only over a connection that verified the pinned bridge.
        if self.link is None:
            raise OSError("No verified Hue bridge connection")
        return self.link.connect(1.5)

    def _drop(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def request(self, method, path, body):
        """Send one request over a kept-alive connection to the bridge.

        A new TLS handshake costs far more than the request itself, so the
        connection is reused. A reused connection that the bridge already
        closed fails before any response; only then is the request sent once
        more on a new connection. Every Hue request here is idempotent.
        """
        if path != self.path:
            raise ValueError("Unexpected Hue resource")
        data = None if body is None else json.dumps(body).encode()
        headers = {"Content-Type": "application/json", "hue-application-key": self.key}
        with self.connection_lock:
            while True:
                reused = self.connection is not None
                if not reused:
                    self.connection = self.connection_factory()
                try:
                    self.connection.request(method, path, body=data, headers=headers)
                    response = self.connection.getresponse()
                    status, raw = response.status, response.read()
                    if response.will_close:
                        self._drop()
                    break
                except STALE_CONNECTION:
                    self._drop()
                    if not reused:
                        raise
                except BaseException:
                    self._drop()
                    raise
        if not 200 <= status < 300:
            raise OSError(f"Native Hue answered HTTP {status}")
        result = json.loads(raw)
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

    def rename(self, name):
        """Set only this pinned light's name; never color, power or any other field."""
        if not isinstance(name, str) or not 1 <= len(name) <= HUE_NAME_MAX:
            raise ValueError("Invalid native Hue light name")
        result = self.requester("PUT", self.path, {"metadata": {"name": name}})
        if result.get("errors") or not any(
            item.get("rid") == self.resource_id for item in result.get("data", [])
        ):
            raise ValueError("Native rename not confirmed")

    def identify(self):
        """Blink only this pinned light (Hue v2 ``identify``); never color or power."""
        result = self.requester("PUT", self.path, {"identify": {"action": "identify"}})
        if result.get("errors") or not any(
            item.get("rid") == self.resource_id for item in result.get("data", [])
        ):
            raise ValueError("Native identify not confirmed")


def native_body(native):
    """A write body that shows a light's read-back colour and brightness again."""
    body = {"dynamics": {"duration": 400}}
    brightness = (native.get("dimming") or {}).get("brightness")
    if type(brightness) in (int, float) and math.isfinite(brightness):
        body["dimming"] = {"brightness": float(brightness)}
    temperature = native.get("color_temperature") or {}
    points = (native.get("gradient") or {}).get("points") or []
    xy = (native.get("color") or {}).get("xy")
    if temperature.get("mirek_valid") is True and type(temperature.get("mirek")) is int:
        body["color_temperature"] = {"mirek": temperature["mirek"]}
    elif 2 <= len(points) <= 5:
        body["gradient"] = {"points": [{"color": {"xy": {"x": p["color"]["xy"]["x"],
                                                          "y": p["color"]["xy"]["y"]}}}
                                       for p in points]}
    elif isinstance(xy, dict):
        body["color"] = {"xy": {"x": xy["x"], "y": xy["y"]}}
    return body


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
        self, client, light, clock=time.monotonic, wall_clock=time.time, layout_store=None,
        preview=None, profile=None,
    ):
        client_profile = getattr(client, "profile", None)
        if any(value is not None and not isinstance(value, _LIGHT_SETS)
               for value in (profile, client_profile)):
            raise ValueError("Invalid native Hue profile")
        if profile is not None and client_profile is not None and profile != client_profile:
            raise ValueError("Conflicting native Hue profiles")
        self.profile = profile if profile is not None else client_profile
        if not isinstance(self.profile, _LIGHT_SETS):
            raise ValueError("An explicit native Hue profile is required")
        self.layout_store = layout_store
        self.preview = preview
        # The registry id of the native light; commands and layout rows use it.
        self.light = light
        resources = self.profile.light_ids
        self.resource_id = getattr(client, "resource_id", None)
        if light not in resources or resources[light] != self.resource_id:
            raise ValueError("Hue light does not match the selected resource")
        self.brightness_scale = default_scale(light)
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
        # What the light showed before a calibration took it over (see remember_light).
        self.remembered = None
        self.healthy = False
        self.writes = 0
        self.last_result = "waiting"
        self.last_payload = None
        # The name this proxy last wrote to, or read from, the physical light.
        self.named = None
        self.name_failed = float("-inf")

    def layout_factor(self):
        """Return the scene correction times the light's scene fade gain."""
        store = self.layout_store
        if store is None:
            return self.brightness_scale
        scale = getattr(store, "scene_scale", store.scale)(self.light, self.brightness_scale)
        loader = getattr(store, "scene_fade", None)
        fade = loader() if loader is not None else None
        return scale * (fade.get(self.light, 1) if fade else 1)

    def submit(self, payload):
        if (
            not isinstance(payload, dict)
            or set(payload)
            != {"light", "state", "transitiontime", "session", "sequence", "issued_at"}
            or payload["light"] != self.light
        ):
            raise ValueError("Invalid Hue command")
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
            and self.layout_store.layout_for(self.light) is not None
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
        return present(self.light) if present is not None else True

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

    def remember_light(self):
        """Keep what the light shows now, so restore_scene can put it back.

        A calibration writes test colours straight to the light. Its scene comes
        back through this proxy only when a scene command came through it since the
        adapter started; a scene set in the Hue app, or none since a restart, would
        otherwise leave the light at the calibration's dark level.
        """
        with self.state_lock:
            native = copy.deepcopy(self.native)
        with self.command_lock:
            self.remembered = native_body(native) if native else None

    def restore_scene(self):
        with self.command_lock:
            remembered, self.remembered = self.remembered, None
            if self.desired is None and remembered is not None:
                if "dimming" in remembered and self.layout_store is not None:
                    # process_once scales a scene brightness by the correction; undo it
                    # so the light gets back exactly the brightness it had.
                    remembered["dimming"]["brightness"] = (
                        remembered["dimming"]["brightness"] * self.brightness_scale
                        / max(self.layout_factor(), 1e-6))
                self.desired = (remembered, self.wall_clock())
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
                    # Nothing to write: the preview shows what the light has now, even a
                    # scene that did not come through this proxy.
                    if self.preview is not None:
                        try:
                            self.preview.record_hue_read_back(self.light, native)
                        except Exception:
                            pass  # Passive telemetry cannot change the output path.
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
                            self.preview.record_hue(self.light, body)
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

    def set_power(self, on, attempts=16, pause=0.05, sleep=time.sleep):
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

    def sync_owner_name(self):
        """Give the physical light the owner's name when Light Studio changed it.

        Nothing is written without an owner name, when the light already has the
        name, or while the name equals the one this proxy last applied or read.
        A name the owner later sets in the Hue app stays until the owner changes
        the name in Light Studio. A failed write is retried every
        NAME_RETRY_SECONDS. Returns True after a write.
        """
        names = getattr(self.layout_store, "names", None)
        name = names().get(self.light) if names is not None else None
        if (name is None or name == self.named
                or self.clock() - self.name_failed < NAME_RETRY_SECONDS):
            return False
        try:
            with self.command_lock:
                current = self.client.read().get("metadata", {}).get("name")
                if current == name:
                    self.named = name
                    return False
                self.client.rename(name)
                self.named = name
                return True
        except (OSError, ValueError, KeyError, IndexError, TypeError, AttributeError):
            self.name_failed = self.clock()
            return False

    def run(self, stop):
        while not stop.is_set():
            self.wake.clear()
            self.process_once()
            self.sync_owner_name()
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
