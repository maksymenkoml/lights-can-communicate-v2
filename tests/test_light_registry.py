"""Validate the light registry model and its one-time migration without devices."""

import json
import os
import stat
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

from lights_can_communicate_v2_core.light_registry import (
    LEGACY_SIDES,
    REGISTRY_FILE,
    LightRegistry,
    RegistryError,
)

ROOT = Path(__file__).resolve().parents[1]
STRIP = "a1111111-1111-4111-8111-111111111111"
LEFT = "b2222222-2222-4222-8222-222222222222"
CENTER = "c3333333-3333-4333-8333-333333333333"
RIGHT = "d4444444-4444-4444-8444-444444444444"
AREA = "e5555555-5555-4555-8555-555555555555"
CENTER_SERVICE = "f6666666-6666-4666-8666-666666666666"
LEFT_SERVICE = "a7777777-7777-4777-8777-777777777777"
RIGHT_SERVICE = "b8888888-8888-4888-8888-888888888888"
STRIP_SERVICE = "c9999999-9999-4999-8999-999999999999"

GOVEE_TARGETS = [
    {"side": "left", "device": "AA:BB:CC:DD:EE:01", "ip": "192.168.10.50",
     "brightness_scale": 0.65, "sku": "H6076", "max_gradient_packets_per_second": 2},
    {"side": "right", "device": "AA:BB:CC:DD:EE:02", "ip": "192.168.10.51",
     "brightness_scale": 0.65, "model": "H6076", "segment_count": 7},
]


def govee_light(light_id, target, id_v1, seed):
    return {"id": light_id, "provider": "govee",
            "virtual": {"id_v1": id_v1, "seed": seed},
            "govee": {key: value for key, value in target.items() if key != "side"}}


def hue_light(light_id, light, channels, id_v1, seed):
    return {"id": light_id, "provider": "hue",
            "virtual": {"id_v1": id_v1, "seed": seed},
            "hue": {"light": light,
                    "channels": [{"channel_id": channel_id, "rid": rid, "index": index}
                                 for channel_id, rid, index in channels]}}


def sample(hue=True):
    lights = [
        govee_light("govee-left", GOVEE_TARGETS[0], "1", "Left"),
        govee_light("govee-right", GOVEE_TARGETS[1], "2", "Right"),
    ]
    if hue:
        lights += [
            hue_light("hue-monitor", STRIP,
                      [(3 + index, STRIP_SERVICE, index) for index in range(9)],
                      "3", "HueMonitorStrip"),
            hue_light("play-left", LEFT, [(1, LEFT_SERVICE, 0)], "4", "HuePlayLeft"),
            hue_light("play-center", CENTER, [(0, CENTER_SERVICE, 0)], "5", "HuePlayCenter"),
            hue_light("play-right", RIGHT, [(2, RIGHT_SERVICE, 0)], "6", "HuePlayRight"),
        ]
    data = {"version": 1, "lights": lights}
    if hue:
        data["hue"] = {"area_id": AREA}
    return data


def example_profile():
    return json.loads((ROOT / "core" / "native-hue-profile.example.json").read_text())


class RegistryModelTests(unittest.TestCase):
    def test_round_trip(self):
        data = sample()
        self.assertEqual(LightRegistry.from_dict(data).to_dict(), data)
        self.assertEqual(LightRegistry.from_dict(data).ids, (
            "govee-left", "govee-right", "hue-monitor", "play-left", "play-center",
            "play-right"))

    def test_registry_is_immutable_and_detached(self):
        data = sample()
        registry = LightRegistry.from_dict(data)
        data["lights"].clear()
        registry.to_dict()["lights"].clear()
        self.assertEqual(registry.to_dict(), sample())
        with self.assertRaises(FrozenInstanceError):
            registry._json = "{}"

    def test_govee_targets_add_side(self):
        registry = LightRegistry.from_dict(sample())
        targets = registry.govee_targets()
        self.assertEqual(targets, GOVEE_TARGETS)
        targets[0]["ip"] = "changed"
        targets.pop()
        self.assertEqual(registry.govee_targets(), GOVEE_TARGETS)
        self.assertEqual(LEGACY_SIDES, {"govee-left": "left", "govee-right": "right"})

    def test_native_profile_matches_source(self):
        profile = example_profile()
        data = sample()
        # The example profile uses its own resources; build the registry from them.
        lights = profile["lights"]
        channels = {item["channel_id"]: item["member"]
                    for item in profile["entertainment"]["channels"]}
        data["hue"] = {"area_id": profile["entertainment"]["area_id"]}
        data["lights"][2:] = [
            hue_light("hue-monitor", lights["strip"],
                      [(cid, channels[cid]["rid"], channels[cid]["index"]) for cid in range(3, 12)],
                      "3", "HueMonitorStrip"),
            hue_light("play-left", lights["hue_left"],
                      [(1, channels[1]["rid"], 0)], "4", "HuePlayLeft"),
            hue_light("play-center", lights["hue_center"],
                      [(0, channels[0]["rid"], 0)], "5", "HuePlayCenter"),
            hue_light("play-right", lights["hue_right"],
                      [(2, channels[2]["rid"], 0)], "6", "HuePlayRight"),
        ]
        self.assertEqual(LightRegistry.from_dict(data).native_profile().to_dict(), profile)

    def test_govee_only_has_no_profile(self):
        registry = LightRegistry.from_dict(sample(hue=False))
        self.assertIsNone(registry.native_profile())
        self.assertEqual(registry.ids, ("govee-left", "govee-right"))

    def test_rejects(self):
        def mutate(change):
            data = sample()
            change(data)
            return data

        cases = {
            "duplicate id": lambda d: d["lights"][1].__setitem__("id", "govee-left"),
            "unknown provider": lambda d: d["lights"][0].__setitem__("provider", "lifx"),
            "missing virtual": lambda d: d["lights"][0].pop("virtual"),
            "duplicate id_v1": lambda d: d["lights"][1]["virtual"].__setitem__("id_v1", "1"),
            "duplicate seed": lambda d: d["lights"][1]["virtual"].__setitem__("seed", "Left"),
            "bad uuid": lambda d: d["lights"][2]["hue"].__setitem__("light", "not-a-uuid"),
            "bad area": lambda d: d["hue"].__setitem__("area_id", "SECRET-AREA"),
            "extra top-level key": lambda d: d.__setitem__("extra", 1),
            "extra light key": lambda d: d["lights"][0].__setitem__("extra", 1),
            "version 2": lambda d: d.__setitem__("version", 2),
            "bool version": lambda d: d.__setitem__("version", True),
            "hue without top-level hue": lambda d: d.pop("hue"),
            "top-level hue without hue lights": lambda d: d["lights"].__delitem__(
                slice(2, None)),
            "three hue lights": lambda d: d["lights"].pop(),
            "unknown govee id": lambda d: d["lights"][0].__setitem__("id", "govee-middle"),
            "govee side key": lambda d: d["lights"][0]["govee"].__setitem__("side", "left"),
            "govee without ip": lambda d: d["lights"][0]["govee"].pop("ip"),
            "single govee light": lambda d: d["lights"].pop(1),
            "channel clash": lambda d: d["lights"][3]["hue"]["channels"][0].__setitem__(
                "channel_id", 0),
            "non-object": lambda d: d.clear() or d.update(lights="SECRET"),
        }
        for name, change in cases.items():
            with self.subTest(name):
                with self.assertRaises(RegistryError) as caught:
                    LightRegistry.from_dict(mutate(change))
                self.assertIsInstance(caught.exception, ValueError)
                for secret in (STRIP, AREA, "SECRET", "192.168.10.50", "AA:BB:CC:DD:EE:01",
                               "not-a-uuid", "lifx", "govee-middle"):
                    self.assertNotIn(secret, str(caught.exception))
        for value in (None, [], "text", 1):
            with self.assertRaises(RegistryError):
                LightRegistry.from_dict(value)

    def test_nan_is_rejected(self):
        data = sample()
        data["lights"][0]["govee"]["brightness_scale"] = float("nan")
        with self.assertRaises(RegistryError):
            LightRegistry.from_dict(data)

    def test_save_is_atomic_and_private(self):
        with tempfile.TemporaryDirectory(prefix="tmp_light_registry_") as directory:
            path = Path(directory) / REGISTRY_FILE
            path.write_text("old")
            os.chmod(path, 0o644)
            registry = LightRegistry.from_dict(sample())
            registry.save(path)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(LightRegistry.load(path), registry)
            self.assertEqual(sorted(item.name for item in Path(directory).iterdir()),
                             [REGISTRY_FILE])

    def test_failed_save_leaves_old_file_and_no_temporary(self):
        with tempfile.TemporaryDirectory(prefix="tmp_light_registry_") as directory:
            path = Path(directory) / REGISTRY_FILE
            path.write_text("old")
            registry = LightRegistry.from_dict(sample())
            real_replace = os.replace

            def failing(source, target):
                raise OSError("disk full SECRET")

            os.replace = failing
            try:
                with self.assertRaises(RegistryError) as caught:
                    registry.save(path)
            finally:
                os.replace = real_replace
            self.assertNotIn("SECRET", str(caught.exception))
            self.assertEqual(path.read_text(), "old")
            self.assertEqual([item.name for item in Path(directory).iterdir()], [REGISTRY_FILE])

    def test_load_rejects_duplicate_keys_and_size(self):
        with tempfile.TemporaryDirectory(prefix="tmp_light_registry_") as directory:
            path = Path(directory) / REGISTRY_FILE
            text = json.dumps(sample())
            path.write_text(text.replace('"version": 1', '"version": 1, "version": 1'))
            with self.assertRaises(RegistryError):
                LightRegistry.load(path)
            data = sample()
            data["lights"][0]["govee"]["padding"] = "x" * (64 * 1024)
            path.write_text(json.dumps(data))
            with self.assertRaises(RegistryError):
                LightRegistry.load(path)
            path.write_text("{not json")
            with self.assertRaises(RegistryError):
                LightRegistry.load(path)
            with self.assertRaises(RegistryError):
                LightRegistry.load(Path(directory) / "missing.json")


if __name__ == "__main__":
    unittest.main()
