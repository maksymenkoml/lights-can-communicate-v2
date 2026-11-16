"""Bounded per-light Play intent, independent of power and Sync state."""

import json
import math
import os
import tempfile
import threading
import uuid
from pathlib import Path

MAX_STATE_BYTES = 4096
VIRTUAL_SLOTS = frozenset(str(slot) for slot in range(1, 7))
MANUAL_FIELDS = frozenset({"on", "bri", "xy", "ct", "hue", "sat", "gradient",
                           "bri_inc", "ct_inc", "hue_inc", "sat_inc", "effect"})
COLOR_MODELS = frozenset({"LCT001", "LCT015", "LST002", "LCX002", "915005987201",
                          "LCX004", "LCX006", "LCA005"})


def _scene_id(value):
    if not isinstance(value, str) or len(value) != 36:
        return False
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _palette_for(lamp, scene):
    """Copy only color data; replay must never introduce a power request."""
    speed = getattr(scene, "speed", None)
    if type(speed) not in (float, int) or not 0 < speed <= 1 or not math.isfinite(speed):
        return None
    palette = getattr(scene, "palette", None)
    if not isinstance(palette, dict):
        return None
    key = ("color" if lamp.modelid in COLOR_MODELS else
           "color_temperature" if lamp.modelid == "LTW001" else "dimming")
    entries = palette.get(key)
    if not isinstance(entries, list) or not 1 <= len(entries) <= 256:
        return None
    copied = []
    for entry in entries:
        if not isinstance(entry, dict):
            return None
        color = {}
        for field in ("color", "color_temperature", "dimming"):
            if field not in entry:
                continue
            value = entry[field]
            if not isinstance(value, dict):
                return None
            if field == "color":
                xy = value.get("xy")
                if (not isinstance(xy, dict)
                        or not all(_number(xy.get(axis), 0, 1) for axis in ("x", "y"))):
                    return None
                color[field] = {"xy": {axis: xy[axis] for axis in ("x", "y")}}
            elif field == "dimming":
                if not _number(value.get("brightness"), 0, 100):
                    return None
                color[field] = {"brightness": value["brightness"]}
            else:
                if not _number(value.get("mirek"), 153, 500):
                    return None
                color[field] = {"mirek": value["mirek"]}
        if key not in color:
            return None
        copied.append(color)
    return {key: copied}


def _number(value, lower, upper):
    return (type(value) in (float, int) and lower <= value <= upper
            and math.isfinite(value))


class ScenePlayIntent:
    def __init__(self, path, selected_slots, guard, lights, scenes):
        self.path = Path(path)
        self.slots = frozenset(selected_slots) & VIRTUAL_SLOTS
        self.guard, self.lights, self.scenes = guard, lights, scenes
        self.intents, self.last_error = {}, None
        self.persisted_count, self.replayed_count, self.skipped_count = 0, 0, 0
        self.local = threading.local()
        self._load()

    def _load(self):
        try:
            with self.path.open("rb") as source:
                raw = source.read(MAX_STATE_BYTES + 1)
            if len(raw) > MAX_STATE_BYTES:
                raise ValueError("Oversize Play intent")
            data = json.loads(raw)
            if (not isinstance(data, dict) or set(data) != {"version", "lights"}
                    or type(data["version"]) is not int or data["version"] != 1
                    or not isinstance(data["lights"], dict) or len(data["lights"]) > 6):
                raise ValueError("Invalid Play intent")
            self.intents = {slot: scene for slot, scene in data["lights"].items()
                            if slot in self.slots and _scene_id(scene)}
            self.persisted_count = len(self.intents)
            self.skipped_count = len(data["lights"]) - len(self.intents)
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, RecursionError) as error:
            self.last_error = type(error).__name__

    def _save(self):
        temporary = None
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(prefix="tmp_scene-play-intent-",
                                                      dir=self.path.parent)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                os.fchmod(output.fileno(), 0o600)
                json.dump({"version": 1, "lights": self.intents}, output,
                          sort_keys=True, separators=(",", ":"))
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
            self.persisted_count = len(self.intents)
            self.last_error = None
        except OSError as error:
            self.last_error = type(error).__name__
            # Prefer losing recovery over reviving an explicitly cancelled Play.
            try:
                self.path.unlink(missing_ok=True)
                self.persisted_count = 0
            except OSError:
                self.persisted_count = None
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
                except OSError as error:
                    self.last_error = type(error).__name__

    def cancel(self, lamps):
        with self.guard.lock:
            changed = False
            for lamp in lamps:
                if lamp is not None and lamp.id_v1 in self.intents:
                    del self.intents[lamp.id_v1]
                    changed = True
            if changed:
                self._save()

    def install(self, scene_module, light_class):
        """Install after the PlayGuard, live-output and power wrappers."""
        activate, constructor = scene_module.Scene.activate, scene_module.Thread
        set_state = light_class._setV1State
        dynamic_method = light_class.dynamicScenePlay
        controller = self

        class IntentThread:
            def __init__(self, thread, lamp, context):
                self.thread, self.lamp, self.context = thread, lamp, context

            def __getattr__(self, name):
                return getattr(self.thread, name)

            def start(self):
                result = self.thread.start()
                self.context.append(self.lamp)
                return result

        def create_thread(*args, **kwargs):
            thread = constructor(*args, **kwargs)
            context = getattr(controller.local, "started", None)
            target = kwargs.get("target")
            if context is not None and getattr(target, "__func__", None) is dynamic_method:
                return IntentThread(thread, target.__self__, context)
            return thread

        def activate_scene(scene, values):
            with controller.guard.lock:
                action = values.get("recall", {}).get("action")
                if action != "dynamic_palette":
                    lamps = ([reference() for reference in scene.lights] if action == "deactivate"
                             else list(scene.lightstates))
                    controller.cancel(lamps)
                    return activate(scene, values)
                previous = getattr(controller.local, "started", None)
                started = []
                controller.local.started = started
                try:
                    return activate(scene, values)
                finally:
                    controller.local.started = previous
                    touched = False
                    for lamp in started:
                        if (lamp.id_v1 not in controller.slots
                                or controller.lights.get(lamp.id_v1) is not lamp):
                            continue
                        touched = True
                        controller.intents.pop(lamp.id_v1, None)
                        if (_scene_id(scene.id_v2)
                                and "dynamic_palette" in lamp.dynamics.get("status_values", ())
                                and _palette_for(lamp, scene) is not None):
                            controller.intents[lamp.id_v1] = scene.id_v2
                    if touched:
                        controller._save()

        def manual_state(lamp, values, advertise=True):
            with controller.guard.lock:
                if (set(values) & MANUAL_FIELDS
                        and controller.guard.worker_generation(lamp) is None):
                    controller.cancel([lamp])
                return set_state(lamp, values, advertise)

        scene_module.Thread = create_thread
        scene_module.Scene.activate = activate_scene
        light_class._setV1State = manual_state
        self.scene_module = scene_module
        return self

    def replay(self):
        """Resume individual workers through the existing guarded thread factory."""
        with self.guard.lock:
            scenes = {scene.id_v2: scene for scene in self.scenes.values()
                      if _scene_id(scene.id_v2)}
            retained, started = {}, []
            replay_error = None
            for slot, scene_id in self.intents.items():
                lamp, scene = self.lights.get(slot), scenes.get(scene_id)
                if (slot not in self.slots or lamp is None or lamp.id_v1 != slot or scene is None
                        or "dynamic_palette" not in lamp.dynamics.get("status_values", ())):
                    continue
                members = [reference() for reference in scene.lights if reference() is not None]
                if lamp not in members:
                    continue
                palette = _palette_for(lamp, scene)
                if palette is None:
                    continue
                # Preserve the scene's original index even when another member cancelled Play.
                index = members.index(lamp) % len(next(iter(palette.values())))
                try:
                    lamp.dynamics["speed"] = scene.speed
                    self.scene_module.Thread(target=lamp.dynamicScenePlay,
                                             args=(palette, index)).start()
                except (OSError, RuntimeError, ValueError) as error:
                    replay_error = type(error).__name__
                    continue
                scene.status = "dynamic_palette"
                retained[slot] = scene_id
                started.append(slot)
            self.replayed_count = len(started)
            self.skipped_count += len(self.intents) - len(retained)
            if retained != self.intents:
                self.intents = retained
                self._save()
            if replay_error is not None and self.last_error is None:
                self.last_error = replay_error
            return started

    def snapshot(self):
        with self.guard.lock:
            return {"remembered_lights": len(self.intents),
                    "persisted_lights": self.persisted_count,
                    "replayed_lights": self.replayed_count,
                    "skipped_lights": self.skipped_count,
                    "last_error": self.last_error}


def install_scene_play_intent(scene_module, light_class, guard, lights, scenes,
                              selected_slots, path):
    return ScenePlayIntent(path, selected_slots, guard, lights, scenes).install(
        scene_module, light_class)
