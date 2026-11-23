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
    ensure,
    migrate,
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
    {"light": "govee-left", "device": "AA:BB:CC:DD:EE:01", "ip": "192.168.10.50",
     "brightness_scale": 0.65, "sku": "H6076", "max_gradient_packets_per_second": 2},
    {"light": "govee-right", "device": "AA:BB:CC:DD:EE:02", "ip": "192.168.10.51",
     "brightness_scale": 0.65, "model": "H6076", "segment_count": 7},
]


# The legacy live targets file keys each lamp by side; only the migration reads it.
LEGACY_TARGETS = [
    {**{key: value for key, value in target.items() if key != "light"},
     "side": LEGACY_SIDES[target["light"]]}
    for target in GOVEE_TARGETS
]


def govee_light(light_id, target, id_v1, seed):
    return {"id": light_id, "provider": "govee",
            "virtual": {"id_v1": id_v1, "seed": seed},
            "govee": {key: value for key, value in target.items() if key not in ("side", "light")}}


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

    def test_govee_targets_carry_the_light_id(self):
        registry = LightRegistry.from_dict(sample())
        targets = registry.govee_targets()
        self.assertEqual(targets, GOVEE_TARGETS)
        targets[0]["ip"] = "changed"
        targets.pop()
        self.assertEqual(registry.govee_targets(), GOVEE_TARGETS)
        self.assertEqual(LEGACY_SIDES, {"govee-left": "left", "govee-right": "right"})

    def test_with_govee_targets_follows_a_new_address(self):
        registry = LightRegistry.from_dict(sample())
        targets = registry.govee_targets()
        targets[1]["ip"] = "192.168.10.99"
        targets[1]["segment_count"] = 7
        updated = registry.with_govee_targets(targets)
        self.assertEqual(updated.govee_targets(), targets)
        self.assertEqual(updated.ids, registry.ids)
        self.assertEqual(updated.native_profile().to_dict(), registry.native_profile().to_dict())
        self.assertEqual(registry.govee_targets(), GOVEE_TARGETS)
        for bad in (None, [], targets[:1], [targets[0], targets[0]],
                    [targets[0], {**targets[1], "light": "govee-middle"}],
                    [targets[0], {**targets[1], "light": "govee-left"}],
                    [targets[0], {**targets[1], "light": 7}],
                    [targets[0], {**targets[1], "ip": ""}]):
            with self.subTest(bad=repr(bad)[:20]), self.assertRaises(RegistryError):
                registry.with_govee_targets(bad)

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

    def test_any_number_of_govee_lights(self):
        first, second = "device-" + "a" * 32, "device-" + "0123456789abcdef" * 2
        for count in (1, 3):
            data = sample(hue=False)
            data["lights"] = data["lights"][:count]
            if count == 3:
                data["lights"].append({
                    "id": first, "provider": "govee",
                    "virtual": {"id_v1": "7", "seed": "Device1"},
                    "govee": {"device": "AA:BB:CC:DD:EE:03", "ip": "192.168.10.52",
                              "model": "H6008"}})
            with self.subTest(count=count):
                registry = LightRegistry.from_dict(data)
                self.assertEqual([item["light"] for item in registry.govee_targets()],
                                 [item["id"] for item in data["lights"]])
        data = sample(hue=False)
        data["lights"] = [{
            "id": light_id, "provider": "govee",
            "virtual": {"id_v1": str(index + 1), "seed": "Device" + str(index)},
            "govee": {"device": "AA:BB:CC:DD:EE:0" + str(index), "ip": "192.168.10.5" + str(index)}}
            for index, light_id in enumerate((first, second))]
        self.assertEqual(LightRegistry.from_dict(data).ids, (first, second))

    def test_hue_only_registry_is_valid(self):
        data = sample()
        data["lights"] = data["lights"][2:]
        self.assertEqual(LightRegistry.from_dict(data).govee_targets(), [])

    def test_basic_tier_override_round_trips(self):
        data = sample(hue=False)
        data["lights"][0]["govee"]["tier"] = "basic"
        registry = LightRegistry.from_dict(data)
        self.assertEqual(registry.govee_targets()[0]["tier"], "basic")
        self.assertEqual(registry.to_dict(), data)

    def test_with_govee_targets_keeps_a_new_device_light(self):
        data = sample(hue=False)
        data["lights"].append({
            "id": "device-" + "a" * 32, "provider": "govee",
            "virtual": {"id_v1": "7", "seed": "Device1"},
            "govee": {"device": "AA:BB:CC:DD:EE:03", "ip": "192.168.10.52"}})
        registry = LightRegistry.from_dict(data)
        targets = registry.govee_targets()
        targets[2]["ip"] = "192.168.10.60"
        self.assertEqual(registry.with_govee_targets(targets).govee_targets(), targets)
        with self.assertRaises(RegistryError):
            registry.with_govee_targets(targets[:2])

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
            "empty registry": lambda d: d.clear() or d.update(version=1, lights=[]),
            "device id with 31 hex digits": lambda d: d["lights"][0].__setitem__(
                "id", "device-" + "a" * 31),
            "device id with upper-case hex": lambda d: d["lights"][0].__setitem__(
                "id", "device-" + "A" * 32),
            "unknown tier": lambda d: d["lights"][0]["govee"].__setitem__("tier", "segmented"),
            "non-text tier": lambda d: d["lights"][0]["govee"].__setitem__("tier", 1),
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


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="tmp_light_registry_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.targets = self.root / "targets.json"
        self.profile = self.root / "native.json"
        self.targets.write_text(json.dumps({"targets": LEGACY_TARGETS}))
        self.profile.write_text(json.dumps(example_profile()))

    def test_migrate_example_round_trips_behavior(self):
        registry = migrate(self.targets, self.profile)
        self.assertEqual(registry.govee_targets(), GOVEE_TARGETS)
        self.assertEqual(registry.native_profile().to_dict(), example_profile())
        self.assertEqual(registry.ids, (
            "govee-left", "govee-right", "hue-monitor", "play-left", "play-center",
            "play-right"))
        virtual = {item["id"]: (item["virtual"]["id_v1"], item["virtual"]["seed"])
                   for item in registry.to_dict()["lights"]}
        self.assertEqual(virtual, {
            "govee-left": ("1", "Left"), "govee-right": ("2", "Right"),
            "hue-monitor": ("3", "HueMonitorStrip"), "play-left": ("4", "HuePlayLeft"),
            "play-center": ("5", "HuePlayCenter"), "play-right": ("6", "HuePlayRight")})

    def test_migrate_keeps_source_order(self):
        self.targets.write_text(json.dumps({"targets": LEGACY_TARGETS[::-1]}))
        self.assertEqual(migrate(self.targets, None).govee_targets(), GOVEE_TARGETS[::-1])

    def test_migrate_does_not_change_old_files(self):
        before = (self.targets.read_bytes(), self.profile.read_bytes())
        migrate(self.targets, self.profile)
        self.assertEqual((self.targets.read_bytes(), self.profile.read_bytes()), before)

    def test_migrate_govee_only(self):
        registry = migrate(self.targets, None)
        self.assertIsNone(registry.native_profile())
        self.assertEqual(registry.ids, ("govee-left", "govee-right"))
        self.assertEqual(registry.govee_targets(), GOVEE_TARGETS)

    def test_migrate_without_targets_fails(self):
        for source in (None, self.root / "missing.json"):
            with self.assertRaises(RegistryError) as caught:
                migrate(source, self.profile)
            self.assertIn("targets file", str(caught.exception))
            self.assertNotIn(str(self.root), str(caught.exception))

    def test_migrate_rejects_unknown_side(self):
        for targets in (
            [{**LEGACY_TARGETS[0], "side": "middle"}, LEGACY_TARGETS[1]],
            [LEGACY_TARGETS[0], LEGACY_TARGETS[0]],
            [LEGACY_TARGETS[0]],
            [LEGACY_TARGETS[0], "SECRET"],
        ):
            with self.subTest(targets=len(targets)):
                self.targets.write_text(json.dumps({"targets": targets}))
                with self.assertRaises(RegistryError) as caught:
                    migrate(self.targets, None)
                self.assertNotIn("SECRET", str(caught.exception))

    def test_migrate_rejects_bad_inputs(self):
        self.profile.write_text("{}")
        with self.assertRaises(RegistryError):
            migrate(self.targets, self.profile)
        self.profile.write_text(json.dumps(example_profile()))
        with self.assertRaises(RegistryError):
            migrate(self.targets, self.root / "missing.json")
        self.targets.write_text("[]")
        with self.assertRaises(RegistryError):
            migrate(self.targets, None)

    def test_ensure_prefers_saved_registry(self):
        first = ensure(self.root, self.targets, self.profile)
        path = self.root / REGISTRY_FILE
        first.save(path)
        self.assertEqual(LightRegistry.load(path), first)
        self.targets.write_text("corrupt")
        self.profile.unlink()
        self.assertEqual(ensure(self.root, self.targets, self.profile), first)
        self.assertEqual(ensure(self.root, None, None), first)

    def test_ensure_does_not_write(self):
        registry = ensure(self.root, self.targets, self.profile)
        self.assertEqual(registry.govee_targets(), GOVEE_TARGETS)
        self.assertFalse((self.root / REGISTRY_FILE).exists())
        self.assertEqual(sorted(item.name for item in self.root.iterdir()),
                         ["native.json", "targets.json"])

    def test_ensure_corrupt_registry_fails_closed(self):
        for content in ("{not json", json.dumps({"version": 1, "lights": []}), ""):
            with self.subTest(content=content[:12]):
                (self.root / REGISTRY_FILE).write_text(content)
                with self.assertRaises(RegistryError):
                    ensure(self.root, self.targets, self.profile)
                self.assertEqual((self.root / REGISTRY_FILE).read_text(), content)

    def test_failed_migration_leaves_no_partial_registry(self):
        self.targets.unlink()
        with self.assertRaises(RegistryError):
            ensure(self.root, self.targets, self.profile)
        self.assertFalse((self.root / REGISTRY_FILE).exists())
        self.assertEqual([item.name for item in self.root.iterdir()], ["native.json"])

    def test_registry_over_size_limit_is_rejected_before_save(self):
        # The targets file stays under the read limit; the migrated registry,
        # with Hue lights and indentation, does not.
        targets = json.loads(self.targets.read_text())
        targets["targets"][0]["note"] = ""
        size = len(json.dumps(targets).encode())
        targets["targets"][0]["note"] = "x" * (64 * 1024 - size - 64)
        self.targets.write_text(json.dumps(targets))
        self.assertLess(self.targets.stat().st_size, 64 * 1024)
        with self.assertRaises(RegistryError):
            ensure(self.root, self.targets, self.profile)
        self.assertFalse((self.root / REGISTRY_FILE).exists())

if __name__ == "__main__":
    unittest.main()
