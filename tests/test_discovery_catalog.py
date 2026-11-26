"""Metadata discovery stays independent of routing and hardware authorization."""

import gc
import io
import json
import threading
import tracemalloc
import unittest
from unittest.mock import patch

try:
    from lights_can_communicate_v2_core.discovery import DeviceCatalog
    from lights_can_communicate_v2_core.hue_discovery import HueDiscoveryReader
except ModuleNotFoundError:
    DeviceCatalog = HueDiscoveryReader = None


MAC = "AA:BB:CC:DD:EE:FF:00:11"
OTHER = "AA:BB:CC:DD:EE:FF:00:22"
THIRD = "AA:BB:CC:DD:EE:FF:00:33"
BRIDGE = "001788FFFE123456"
LIGHT = "11111111-1111-4111-8111-111111111111"
DEVICE = "22222222-2222-4222-8222-222222222222"


def hue(model="LCX005", name="Desk", light=LIGHT):
    return (
        [{"id": light, "type": "light", "owner": {"rid": DEVICE, "rtype": "device"},
          "metadata": {"name": name}}],
        [{"id": DEVICE, "type": "device", "product_data": {"model_id": model}}],
    )


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(DeviceCatalog, "metadata catalog is not implemented")
        self.now = 100
        self.catalog = DeviceCatalog(clock=lambda: self.now, ttl=20)

    def scan(self, device=MAC, ip="192.168.1.10", **metadata):
        self.catalog.observe_govee({"device": device, "sku": "H6076", **metadata}, ip)

    def one(self):
        rows = self.catalog.snapshot()["devices"]
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_an_online_unambiguous_govee_light_of_any_model_is_enrollable(self):
        self.catalog.observe_govee({"device": MAC.lower(), "sku": "h6008"}, "192.168.1.10")
        identifier = self.one()["id"]
        self.assertFalse(self.one()["supported"])
        self.assertEqual(self.catalog.govee_enrollment(identifier),
                         {"device": MAC, "ip": "192.168.1.10", "model": "H6008"})
        self.now = 121  # stale: offline
        self.assertIsNone(self.catalog.govee_enrollment(identifier))
        self.assertIsNone(self.catalog.govee_enrollment("device-" + "0" * 32))

    def test_ambiguous_migrated_or_hue_lights_are_not_enrollable(self):
        self.scan()
        self.scan(device=OTHER, ip="192.168.1.10")  # two devices on one address
        self.assertEqual([self.catalog.govee_enrollment(row["id"])
                          for row in self.catalog.snapshot()["devices"]], [None, None])
        legacy = DeviceCatalog(clock=lambda: self.now, ttl=20,
                               legacy_roles={("govee", MAC): "govee-left"})
        legacy.observe_govee({"device": MAC, "sku": "H6076"}, "192.168.1.10")
        row = legacy.snapshot()["devices"][0]
        self.assertIsNone(legacy.govee_enrollment(row["id"]))
        self.catalog.observe_hue(BRIDGE, *hue())
        hue_row = next(row for row in self.catalog.snapshot()["devices"]
                       if row["provider"] == "hue")
        self.assertIsNone(self.catalog.govee_enrollment(hue_row["id"]))

    def test_an_online_unambiguous_hue_light_is_enrollable(self):
        self.catalog.observe_hue(BRIDGE, *hue())
        identifier = self.one()["id"]
        self.assertEqual(self.catalog.hue_enrollment(identifier),
                         {"light": LIGHT, "model": "LCX005"})
        self.assertIsNone(self.catalog.govee_enrollment(identifier))
        self.now = 121  # stale: offline
        self.assertIsNone(self.catalog.hue_enrollment(identifier))
        self.assertIsNone(self.catalog.hue_enrollment("device-" + "0" * 32))
        self.scan()
        govee = next(row["id"] for row in self.catalog.snapshot()["devices"]
                     if row["provider"] == "govee")
        self.assertIsNone(self.catalog.hue_enrollment(govee))

    def test_ambiguous_or_migrated_hue_lights_are_not_enrollable(self):
        lights, devices = hue()
        self.catalog.observe_hue(BRIDGE, lights * 2, devices)
        self.assertIsNone(self.catalog.hue_enrollment(self.one()["id"]))
        legacy = DeviceCatalog(clock=lambda: self.now, ttl=20,
                               legacy_roles={("hue", LIGHT): "hue-monitor"})
        legacy.observe_hue(BRIDGE, *hue())
        self.assertIsNone(legacy.hue_enrollment(legacy.snapshot()["devices"][0]["id"]))
        unavailable = DeviceCatalog(clock=lambda: self.now, ttl=20)
        unavailable.observe_hue(BRIDGE, *hue())
        unavailable.provider_state("hue", "unavailable")
        self.assertIsNone(unavailable.hue_enrollment(
            unavailable.snapshot()["devices"][0]["id"]))

    def test_a_removed_migrated_lamp_is_found_under_its_row_and_enrollable(self):
        # The registry keeps a removed Govee lamp's device key without separators.
        catalog = DeviceCatalog(clock=lambda: self.now, ttl=20, rejoin_roles={
            ("govee", MAC.replace(":", "").upper()): "govee-left",
            ("hue", LIGHT): "play-center"})
        catalog.observe_govee({"device": MAC, "sku": "H6076"}, "192.168.1.10")
        catalog.observe_hue(BRIDGE, *hue())
        rows = {row["provider"]: row for row in catalog.snapshot()["devices"]}
        self.assertEqual((rows["govee"]["layout_id"], rows["hue"]["layout_id"]),
                         ("govee-left", "play-center"))
        self.assertIsNotNone(catalog.govee_enrollment(rows["govee"]["id"]))
        self.assertIsNotNone(catalog.hue_enrollment(rows["hue"]["id"]))

    def test_identity_survives_name_case_and_address_change(self):
        self.scan(device=MAC.lower(), deviceName="First")
        original = self.one()["id"]
        self.now = 121
        self.scan(ip="192.168.1.11", deviceName="Renamed")
        row = self.one()
        self.assertRegex(original, r"^device-[0-9a-f]{32}$")
        self.assertEqual(row["id"], original)
        self.assertEqual(row["name"], "Renamed")
        self.assertTrue(row["selectable"])

    def test_missing_govee_names_include_distinct_stable_model_and_mac_suffix(self):
        self.scan()
        self.scan(device=OTHER, ip="192.168.1.20")
        before = {row["id"]: row["name"] for row in self.catalog.snapshot()["devices"]}
        self.assertEqual(len(set(before.values())), 2)
        self.assertTrue(all("H6076" in name for name in before.values()))
        self.assertTrue(any(name.endswith("EEFF0011") for name in before.values()))
        self.assertTrue(any(name.endswith("EEFF0022") for name in before.values()))
        self.now = 121
        self.scan(ip="192.168.1.30")
        after = {row["id"]: row["name"] for row in self.catalog.snapshot()["devices"]}
        self.assertEqual(after, before)
        for name in after.values():
            self.assertLessEqual(len(name), 80)
            self.assertTrue(name.isprintable())
            self.assertNotIn(MAC, name)
            self.assertNotIn(OTHER, name)
            self.assertNotIn("192.168.", name)

    def test_real_govee_name_survives_later_scan_without_optional_name(self):
        self.scan(deviceName="Living room")
        self.scan(ip="192.168.1.30")
        self.assertEqual(self.one()["name"], "Living room")

    def test_profiles_expose_configuration_without_output_permission(self):
        self.scan()
        row = self.one()
        self.assertEqual((row["shape"], row["zones"], row["height_cm"], row["width_cm"]),
                         ("floor", 7, 136, None))
        for model, shape, zones, width in (
            ("440400982841", "bar", 1, 25), ("440400982842", "bar", 1, 25),
            ("LCX005", "strip", 9, None), ("LCX006", "strip", 9, None),
        ):
            with self.subTest(model=model):
                catalog = DeviceCatalog()
                catalog.observe_hue(BRIDGE, *hue(model))
                item = catalog.snapshot()["devices"][0]
                self.assertEqual((item["shape"], item["zones"], item["width_cm"]),
                                 (shape, zones, width))
                self.assertTrue(item["supported"])
                self.assertNotIn("output", item)

    def test_the_table_lamp_two_is_a_one_zone_table_lamp(self):
        self.scan(sku="H6022")
        item = self.one()
        self.assertEqual((item["shape"], item["zones"], item["height_cm"], item["width_cm"]),
                         ("table", 1, 26, 16))
        self.assertTrue(item["supported"])
        self.assertTrue(item["selectable"])

    def test_unknown_models_and_plugs_remain_visible_without_color_capability(self):
        self.scan(sku="H5080")
        item = self.one()
        self.assertFalse(item["supported"])
        self.assertFalse(item["selectable"])
        self.assertEqual((item["shape"], item["zones"]), (None, None))
        self.catalog.observe_hue(BRIDGE, *hue("LOM001", "Plug"))
        self.assertTrue(all(not item["supported"] for item in self.catalog.snapshot()["devices"]))

    def test_hue_identity_is_bridge_scoped_and_name_independent(self):
        self.catalog.observe_hue(BRIDGE, *hue())
        original = self.one()["id"]
        self.catalog.observe_hue(BRIDGE.lower(), *hue(name="Renamed"))
        self.assertEqual(self.one()["id"], original)
        self.catalog.observe_hue("001788FFFE654321", *hue())
        self.assertEqual(len(self.catalog.snapshot()["devices"]), 2)

    def test_collision_blocks_only_affected_identities_then_recovers(self):
        self.scan()
        self.scan(device=OTHER)
        self.scan(device=THIRD, ip="192.168.1.30")
        rows = self.catalog.snapshot()["devices"]
        self.assertEqual(sum(item["ambiguous"] for item in rows), 2)
        self.assertEqual(sum(item["selectable"] for item in rows), 1)
        self.now = 121
        self.scan()
        rows = self.catalog.snapshot()["devices"]
        self.assertEqual(sum(item["selectable"] for item in rows), 1)
        self.assertFalse(any(item["ambiguous"] for item in rows))

    def test_one_identity_at_multiple_addresses_is_ambiguous_with_stable_id(self):
        self.scan()
        original = self.one()["id"]
        self.scan(ip="192.168.1.11")
        self.assertEqual(self.one()["id"], original)
        self.assertTrue(self.one()["ambiguous"])
        self.assertFalse(self.one()["selectable"])

    def test_malformed_identity_blocks_only_known_peer_until_ttl_despite_valid_rescan(self):
        self.scan(deviceName="Affected")
        self.scan(device=OTHER, ip="192.168.1.20", deviceName="Unrelated")
        original = {row["name"]: row["id"] for row in self.catalog.snapshot()["devices"]}
        self.now = 101
        self.catalog.observe_govee({"device": "invalid"}, "192.168.1.10")
        rows = {row["name"]: row for row in self.catalog.snapshot()["devices"]}
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows["Affected"]["ambiguous"])
        self.assertFalse(rows["Affected"]["selectable"])
        self.assertTrue(rows["Unrelated"]["selectable"])
        self.assertFalse(rows["Unrelated"]["ambiguous"])
        for now in (102, 121):
            self.now = now
            self.scan()
            self.scan(device=OTHER, ip="192.168.1.20")
            rows = {row["name"]: row for row in self.catalog.snapshot()["devices"]}
            self.assertTrue(rows["Affected"]["ambiguous"])
            self.assertFalse(rows["Affected"]["selectable"])
            self.assertTrue(rows["Unrelated"]["selectable"])
        self.now = 121.01
        self.scan()
        rows = {row["name"]: row for row in self.catalog.snapshot()["devices"]}
        self.assertFalse(rows["Affected"]["ambiguous"])
        self.assertTrue(rows["Affected"]["selectable"])
        self.assertEqual({name: row["id"] for name, row in rows.items()}, original)

    def test_malformed_peer_before_first_valid_identity_blocks_through_exact_ttl(self):
        self.now = 99.9
        self.catalog.observe_govee({"device": "invalid"}, "192.168.1.10")
        self.assertEqual(self.catalog.snapshot()["devices"], [])
        self.now = 100
        self.scan(deviceName="Affected")
        self.scan(device=OTHER, ip="192.168.1.20", deviceName="Unrelated")
        original = {row["name"]: row["id"] for row in self.catalog.snapshot()["devices"]}
        for now in (100, 100.1, 119.9):
            self.now = now
            self.scan()
            self.scan(device=OTHER, ip="192.168.1.20")
            rows = {row["name"]: row for row in self.catalog.snapshot()["devices"]}
            self.assertTrue(rows["Affected"]["ambiguous"])
            self.assertFalse(rows["Affected"]["selectable"])
            self.assertTrue(rows["Unrelated"]["selectable"])
            self.assertFalse(rows["Unrelated"]["ambiguous"])
        self.now = 119.91
        self.scan()
        rows = {row["name"]: row for row in self.catalog.snapshot()["devices"]}
        self.assertTrue(rows["Affected"]["selectable"])
        self.assertFalse(rows["Affected"]["ambiguous"])
        self.assertEqual({name: row["id"] for name, row in rows.items()}, original)

    def test_contradictory_peer_pressure_has_bounded_memory_and_fails_closed_until_ttl(self):
        self.catalog = DeviceCatalog(clock=lambda: self.now, ttl=20, max_devices=2)
        tracemalloc.start()
        try:
            for suffix in range(1000):
                self.catalog.observe_govee({"device": "invalid"},
                                          f"192.168.{suffix // 250}.{suffix % 250 + 1}")
            gc.collect()
            before = tracemalloc.get_traced_memory()[0]
            for suffix in range(1000, 4000):
                self.catalog.observe_govee({"device": "invalid"},
                                          f"192.168.{suffix // 250}.{suffix % 250 + 1}")
            gc.collect()
            self.assertLess(tracemalloc.get_traced_memory()[0] - before, 100_000)
        finally:
            tracemalloc.stop()
        self.assertEqual(self.catalog.snapshot()["devices"], [])
        self.scan(ip="192.168.0.1", deviceName="First contradictory peer")
        self.scan(device=OTHER, ip="192.168.15.250", deviceName="Last contradictory peer")
        original = {row["id"] for row in self.catalog.snapshot()["devices"]}
        for now in (100, 119.9, 120):
            self.now = now
            self.scan(ip="192.168.0.1")
            self.scan(device=OTHER, ip="192.168.15.250")
            rows = self.catalog.snapshot()["devices"]
            self.assertTrue(all(row["ambiguous"] for row in rows))
            self.assertTrue(all(not row["selectable"] for row in rows))
        self.now = 120.01
        self.scan(ip="192.168.0.1")
        self.scan(device=OTHER, ip="192.168.15.250")
        rows = self.catalog.snapshot()["devices"]
        self.assertEqual({row["id"] for row in rows}, original)
        self.assertTrue(all(row["selectable"] for row in rows))
        self.assertTrue(all(not row["ambiguous"] for row in rows))

    def test_peer_address_is_used_instead_of_advertised_ip(self):
        self.scan(ip="192.168.1.10")
        self.catalog.observe_govee({"device": OTHER, "sku": "H6076", "ip": "192.168.1.10"},
                                  "192.168.1.20")
        self.assertTrue(all(item["selectable"] for item in self.catalog.snapshot()["devices"]))

    def test_stale_and_provider_failure_keep_identity_but_remove_selection(self):
        self.scan()
        original = self.one()["id"]
        self.catalog.provider_state("govee", "unavailable")
        self.assertFalse(self.one()["online"])
        self.catalog.provider_state("govee", "ready")
        self.assertTrue(self.one()["online"])
        self.now = 121
        row = self.one()
        self.assertEqual(row["id"], original)
        self.assertFalse(row["online"])
        self.assertFalse(row["selectable"])

    def test_snapshots_are_copied_bounded_and_do_not_expose_private_fields(self):
        self.scan(deviceName="\x00\n" + "Lamp" * 100, sku="H6076", hue_key="secret")
        snapshot = self.catalog.snapshot()
        row = self.one()
        self.assertLessEqual(len(row["name"]), 80)
        self.assertNotIn("\x00", row["name"])
        encoded = json.dumps(snapshot)
        for private in (MAC, "192.168.1.10", "secret", "hue_key"):
            self.assertNotIn(private, encoded)
        snapshot["devices"][0]["name"] = "Poisoned"
        snapshot["providers"]["govee"] = "Poisoned"
        self.assertNotEqual(self.one()["name"], "Poisoned")
        self.assertEqual(self.catalog.snapshot()["providers"]["govee"], "ready")

    def test_legacy_hint_never_replaces_identity(self):
        self.catalog = DeviceCatalog({("govee", MAC): "govee-left", ("hue", LIGHT): "hue-strip"})
        self.scan()
        self.catalog.observe_hue(BRIDGE, *hue())
        rows = self.catalog.snapshot()["devices"]
        self.assertEqual({row["layout_id"] for row in rows}, {"govee-left", "hue-strip"})
        self.assertTrue(all(row["id"].startswith("device-") for row in rows))

    def test_malformed_metadata_cannot_poison_valid_unrelated_entries(self):
        self.scan()
        for data in (None, [], {}, {"device": []}, {"device": "govee-left"},
                     {"device": "AA:" * 10000}):
            self.catalog.observe_govee(data, "192.168.1.20")
        for lights, devices in ((None, None), ([None], [{}]),
                                ([{"id": LIGHT, "owner": []}], [])):
            self.catalog.observe_hue(BRIDGE, lights, devices)
        self.catalog.observe_hue("bad bridge", *hue())
        self.assertTrue(self.one()["selectable"])

    def test_missing_hue_owner_model_is_unsupported_not_guessed(self):
        lights, _ = hue()
        self.catalog.observe_hue(BRIDGE, lights, [])
        self.assertFalse(self.one()["supported"])

    def test_duplicate_hue_resource_is_ambiguous_until_clean_poll(self):
        lights, devices = hue()
        self.catalog.observe_hue(BRIDGE, lights * 2, devices)
        self.assertTrue(self.one()["ambiguous"])
        self.catalog.observe_hue(BRIDGE, lights, devices)
        self.assertTrue(self.one()["selectable"])

    def test_catalog_capacity_stale_eviction_and_observation_memory_are_bounded(self):
        self.catalog = DeviceCatalog(clock=lambda: self.now, ttl=20, max_devices=2)
        for suffix in range(100):
            self.scan(device=f"AA:BB:CC:DD:EE:FF:00:{suffix:02X}", ip="192.168.1.10")
        self.assertEqual(len(self.catalog.snapshot()["devices"]), 2)
        self.assertTrue(all(item["ambiguous"] for item in self.catalog.snapshot()["devices"]))
        self.now = 121
        self.scan(device=THIRD, ip="192.168.1.30")
        self.assertEqual(len(self.catalog.snapshot()["devices"]), 2)
        self.assertEqual(sum(item["selectable"] for item in self.catalog.snapshot()["devices"]), 1)

    def test_deterministic_order_is_independent_of_observation_order(self):
        other = DeviceCatalog(clock=lambda: self.now)
        self.scan(device=OTHER, ip="192.168.1.20")
        self.scan()
        other.observe_govee({"device": MAC, "sku": "H6076"}, "192.168.1.10")
        other.observe_govee({"device": OTHER, "sku": "H6076"}, "192.168.1.20")
        self.assertEqual(self.catalog.snapshot(), other.snapshot())

    def test_collision_remains_blocked_when_address_history_reaches_capacity(self):
        self.scan()
        self.scan(device=OTHER)
        for suffix in range(20, 40):
            self.scan(ip=f"192.168.1.{suffix}")
        self.assertTrue(all(item["ambiguous"] for item in self.catalog.snapshot()["devices"]))
        self.assertTrue(all(not item["selectable"] for item in self.catalog.snapshot()["devices"]))

    def test_provider_states_are_fixed_and_cannot_include_diagnostics(self):
        for state in ("discovering", "not_configured", "stale", "ready", "unavailable"):
            self.catalog.provider_state("hue", state)
            self.assertEqual(self.catalog.snapshot()["providers"]["hue"], state)
        self.catalog.provider_state("hue", "key-secret exception")
        self.catalog.provider_state("unbounded-provider", "ready")
        self.assertEqual(self.catalog.snapshot()["providers"],
                         {"govee": "unavailable", "hue": "unavailable"})

    def test_reachable_catalog_stays_connected_when_all_providers_are_unavailable(self):
        self.assertTrue(self.catalog.snapshot()["connected"])
        self.catalog.provider_state("hue", "discovering")
        self.assertTrue(self.catalog.snapshot()["connected"])
        self.scan()
        self.catalog.provider_state("govee", "unavailable")
        self.catalog.provider_state("hue", "unavailable")
        snapshot = self.catalog.snapshot()
        self.assertTrue(snapshot["connected"])
        self.assertFalse(snapshot["devices"][0]["online"])
        self.assertFalse(snapshot["devices"][0]["selectable"])

    def test_address_history_memory_remains_bounded_under_one_identity_flood(self):
        tracemalloc.start()
        try:
            for suffix in range(1000):
                self.scan(ip=f"192.168.{suffix // 250}.{suffix % 250 + 1}")
            gc.collect()
            before = tracemalloc.get_traced_memory()[0]
            for suffix in range(1000, 4000):
                self.scan(ip=f"192.168.{suffix // 250}.{suffix % 250 + 1}")
            gc.collect()
            after = tracemalloc.get_traced_memory()[0]
            self.assertLess(after - before, 100_000)
            self.assertTrue(self.one()["ambiguous"])
        finally:
            tracemalloc.stop()

    def test_ttl_boundary_and_clock_reversal_cannot_make_stale_identity_selectable(self):
        self.scan()
        self.now = 120
        self.assertTrue(self.one()["online"])
        self.now = 120.01
        self.assertFalse(self.one()["online"])
        self.now = 99
        self.assertFalse(self.one()["online"])
        self.assertFalse(self.one()["selectable"])

    def test_invalid_addresses_are_ignored_and_profiles_are_provider_specific(self):
        for address in (None, [], "8.8.8.8", "224.0.0.1", "0.0.0.0", "host.example"):
            self.catalog.observe_govee({"device": MAC, "sku": "H6076"}, address)
        self.assertEqual(self.catalog.snapshot()["devices"], [])
        self.scan(sku="LCX005")
        self.catalog.observe_hue(BRIDGE, *hue("H6076"))
        self.assertTrue(all(not item["supported"] for item in self.catalog.snapshot()["devices"]))

    def test_catalog_rejects_unbounded_configuration(self):
        for options in ({"ttl": 0}, {"ttl": float("nan")}, {"max_devices": 0},
                        {"max_devices": 100_000}, {"max_devices": True}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                DeviceCatalog(**options)


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(HueDiscoveryReader, "paired Hue reader is not implemented")
        self.catalog = DeviceCatalog()
        lights, devices = hue()
        self.responses = {
            "/clip/v2/resource/bridge": {"data": [{"bridge_id": BRIDGE}], "errors": []},
            "/clip/v2/resource/device": {"data": devices, "errors": []},
            "/clip/v2/resource/light": {"data": lights, "errors": []},
        }
        self.paths = []

    def request(self, path):
        self.paths.append(path)
        return self.responses[path]

    def reader(self, **options):
        return HueDiscoveryReader({"bridge": "192.168.1.2", "hue_key": "paired-secret"},
                                  self.catalog, requester=self.request, **options)

    def test_success_reads_only_three_metadata_resources(self):
        self.assertTrue(self.reader().poll_once())
        self.assertEqual(self.paths, ["/clip/v2/resource/bridge", "/clip/v2/resource/device",
                                      "/clip/v2/resource/light"])
        snapshot = self.catalog.snapshot()
        self.assertEqual(snapshot["providers"]["hue"], "ready")
        self.assertTrue(snapshot["devices"][0]["selectable"])

    def test_failure_keeps_offline_entries_and_does_not_leak_error(self):
        reader = self.reader()
        self.assertTrue(reader.poll_once())
        self.responses["/clip/v2/resource/device"] = {"errors": [{"description": "paired-secret"}]}
        self.assertFalse(reader.poll_once())
        snapshot = self.catalog.snapshot()
        self.assertEqual(snapshot["providers"]["hue"], "unavailable")
        self.assertFalse(snapshot["devices"][0]["online"])
        self.assertNotIn("paired-secret", json.dumps(snapshot))

    def test_malformed_responses_and_request_failures_are_contained(self):
        for value in (None, [], {"data": {}}, {"data": [], "errors": "bad"},
                      {"data": [{"bridge_id": "not a bridge"}], "errors": []}):
            with self.subTest(value=value):
                self.responses["/clip/v2/resource/bridge"] = value
                self.assertFalse(self.reader().poll_once())
        def broken(path):
            raise OSError("paired-secret")
        self.assertFalse(HueDiscoveryReader({"bridge": "192.168.1.2", "hue_key": "key"},
                                           self.catalog, requester=broken).poll_once())

    def test_run_honors_stop_before_and_between_requests(self):
        stop = threading.Event()
        stop.set()
        self.reader().run(stop)
        self.assertEqual(self.paths, [])
        stop.clear()
        def stopping(path):
            stop.set()
            return self.request(path)
        reader = HueDiscoveryReader({"bridge": "192.168.1.2", "hue_key": "key"},
                                    self.catalog, requester=stopping)
        reader.run(stop)
        self.assertEqual(self.paths, ["/clip/v2/resource/bridge"])
        self.assertEqual(self.catalog.snapshot()["devices"], [])

    def test_constructor_performs_no_file_or_socket_io_and_validates_local_config(self):
        with patch("builtins.open", side_effect=AssertionError("file IO")), \
                patch("socket.socket", side_effect=AssertionError("socket IO")):
            self.reader()
        for config in ({}, {"bridge": "https://remote.example", "hue_key": "key"},
                       {"bridge": "8.8.8.8", "hue_key": "key"},
                       {"bridge": "192.168.1.2", "hue_key": "bad\nkey"}):
            with self.subTest(config=config), self.assertRaises(ValueError):
                HueDiscoveryReader(config, self.catalog)

    def connection(self, answer, status=200):
        """A verified bridge connection: ``answer(path)`` gives the body bytes."""
        sent = []

        class Connection:
            closed = False

            def request(self, method, path, body=None, headers=None):
                sent.append((method, path, body, headers))

            def getresponse(self):
                raw = answer(sent[-1][1])
                return type("Response", (), {
                    "status": status, "read1": lambda _self, count: raw.read(count)})()

            def close(self):
                Connection.closed = True

        return Connection, sent

    def test_real_transport_uses_bounded_gets_over_the_verified_connection(self):
        responses = self.responses
        Connection, sent = self.connection(
            lambda path: io.BytesIO(json.dumps(responses[path]).encode()))
        timeouts = []

        def connect(timeout):
            timeouts.append(timeout)
            return Connection()

        reader = HueDiscoveryReader({"bridge": "192.168.1.2", "hue_key": "paired-secret"},
                                    self.catalog, connect=connect)
        self.assertTrue(reader.poll_once())
        self.assertEqual([call[1] for call in sent],
                         ["/clip/v2/resource/" + name for name in ("bridge", "device", "light")])
        for method, _, body, headers in sent:
            self.assertEqual((method, body), ("GET", None))
            self.assertEqual(headers["hue-application-key"], "paired-secret")
        self.assertEqual(timeouts, [1.5] * 3)
        self.assertTrue(Connection.closed)

    def test_without_a_verified_connection_the_key_is_never_sent(self):
        reader = HueDiscoveryReader({"bridge": "192.168.1.2", "hue_key": "paired-secret"},
                                    self.catalog)
        self.assertFalse(reader.poll_once())
        self.assertEqual(self.catalog.snapshot()["providers"]["hue"], "unavailable")

    def test_real_transport_bounds_response_size_and_slow_reads(self):
        class Endless:
            def read(self, count):
                return b" " * count

        Connection, _ = self.connection(lambda path: Endless())
        reader = HueDiscoveryReader({"bridge": "192.168.1.2", "hue_key": "paired-secret"},
                                    self.catalog, connect=lambda timeout: Connection())
        self.assertFalse(reader.poll_once())
        with patch("lights_can_communicate_v2_core.hue_discovery.time.monotonic",
                   side_effect=[0, 0, 2]):
            self.assertFalse(reader.poll_once())
        self.assertEqual(self.catalog.snapshot()["providers"]["hue"], "unavailable")

    def test_http_error_closes_the_connection_and_keeps_public_failure_safe(self):
        self.catalog.observe_hue(BRIDGE, *hue())
        Connection, _ = self.connection(lambda path: io.BytesIO(b"paired-secret"), status=403)
        reader = HueDiscoveryReader({"bridge": "192.168.1.2", "hue_key": "paired-secret"},
                                    self.catalog, connect=lambda timeout: Connection())
        self.assertFalse(reader.poll_once())
        self.assertTrue(Connection.closed)
        snapshot = self.catalog.snapshot()
        self.assertEqual(snapshot["providers"]["hue"], "unavailable")
        self.assertFalse(snapshot["devices"][0]["online"])
        self.assertNotIn("paired-secret", json.dumps(snapshot))

if __name__ == "__main__":
    unittest.main()
