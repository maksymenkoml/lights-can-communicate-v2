"""Persistent native locations retain excluded services without inventing metadata."""

import copy
import json
import os
import stat
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "experiments" / "hue-bridge"))
from native_location_cache import NativeLocationCache  # noqa: E402

AREA = "a1111111-1111-4111-8111-111111111111"
SERVICES = tuple(f"b2222222-2222-4222-8222-22222222222{i}" for i in range(4))
UNKNOWN = "c3333333-3333-4333-8333-333333333333"


def locations():
    return {
        rid: {
            "service": {"rid": rid, "rtype": "entertainment"},
            "positions": [{"x": index / 4, "y": 0, "z": -1}],
            "equalization_factor": index / 4,
        }
        for index, rid in enumerate(SERVICES)
    }


def document(rows=None):
    return {"version": 1, "area_id": AREA, "locations": list((rows or locations()).values())}


class NativeLocationCacheTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "locations.json"

    def cache(self):
        return NativeLocationCache(self.path, AREA, SERVICES)

    def assert_sanitized(self, error):
        for secret in (str(self.path), AREA, *SERVICES, UNKNOWN, "private-failure"):
            self.assertNotIn(secret, str(error))

    def test_missing_constructor_and_empty_merge_do_not_write(self):
        self.path = self.path.parent / "missing" / "locations.json"
        cache = self.cache()
        self.assertEqual(cache.snapshot(), {})
        cache.merge({})
        self.assertFalse(self.path.parent.exists())

    def test_full_then_subset_retains_excluded_service_and_equalization_after_restart(self):
        cache = self.cache()
        expected = locations()
        cache.merge(expected)
        subset = {rid: copy.deepcopy(expected[rid]) for rid in SERVICES[:3]}
        del subset[SERVICES[0]]["equalization_factor"]
        subset[SERVICES[0]]["positions"][0]["x"] = -0.75
        expected[SERVICES[0]]["positions"][0]["x"] = -0.75
        cache.merge(subset)
        self.assertEqual(cache.snapshot(), expected)
        self.assertEqual(self.cache().snapshot(), expected)
        self.assertEqual(json.loads(self.path.read_text()), document(expected))

    def test_does_not_invent_missing_services_or_equalization(self):
        observed = {SERVICES[0]: locations()[SERVICES[0]]}
        del observed[SERVICES[0]]["equalization_factor"]
        cache = self.cache()
        cache.merge(observed)
        self.assertEqual(cache.snapshot(), observed)
        self.assertEqual(self.cache().snapshot(), observed)

    def test_input_and_snapshots_are_deep_copied(self):
        cache = self.cache()
        original = locations()
        cache.merge(original)
        original[SERVICES[0]]["positions"][0]["x"] = -1
        snapshot = cache.snapshot()
        snapshot[SERVICES[0]]["positions"].clear()
        snapshot[SERVICES[1]]["service"]["rid"] = UNKNOWN
        self.assertEqual(cache.snapshot(), locations())

    def test_numeric_boundaries_and_twelve_positions_are_accepted(self):
        observed = locations()
        observed[SERVICES[0]]["positions"] = [{"x": -1, "y": 0.0, "z": 1}] * 12
        observed[SERVICES[0]]["equalization_factor"] = 0
        observed[SERVICES[1]]["equalization_factor"] = 1
        cache = self.cache()
        cache.merge(observed)
        self.assertEqual(self.cache().snapshot(), observed)

    def test_partial_writes_finish_in_private_temporary_file(self):
        original_write = os.write

        def partial_write(descriptor, data):
            self.assertEqual(stat.S_IMODE(os.fstat(descriptor).st_mode), 0o600)
            self.assertFalse(self.path.exists())
            return original_write(descriptor, data[:10])

        cache = self.cache()
        with patch("native_location_cache.os.write", side_effect=partial_write):
            cache.merge(locations())
        self.assertEqual(self.cache().snapshot(), locations())

    def test_unchanged_content_does_not_rewrite_including_after_restart(self):
        cache = self.cache()
        cache.merge(locations())
        before = self.path.stat().st_mtime_ns
        restarted = self.cache()
        with patch("native_location_cache.os.replace") as replace:
            cache.merge(dict(reversed(list(locations().items()))))
            restarted.merge(locations())
            replace.assert_not_called()
        self.assertEqual(self.path.stat().st_mtime_ns, before)

    def test_invalid_documents_fail_closed_without_writing(self):
        cases = ["{", "[]", "null", "true"]
        valid = document()
        for field, value in (("version", True), ("version", 2), ("area_id", UNKNOWN),
                             ("locations", {})):
            changed = copy.deepcopy(valid)
            changed[field] = value
            cases.append(json.dumps(changed))
        changed = copy.deepcopy(valid)
        changed["unexpected"] = "private-failure"
        cases.append(json.dumps(changed))
        changed = copy.deepcopy(valid)
        changed["locations"].append(changed["locations"][0])
        cases.append(json.dumps(changed))
        changed = copy.deepcopy(valid)
        changed["locations"][0]["service"]["rid"] = UNKNOWN
        cases.append(json.dumps(changed))
        cases.extend([
            json.dumps(valid).replace('"version": 1', '"version": 1, "version": 1'),
            json.dumps(valid).replace('"x": 0.0', '"x": 0.0, "x": 0.0'),
            " " * 16385,
        ])
        for raw in cases:
            with self.subTest(raw=raw[:80]):
                self.path.write_text(raw)
                before = self.path.read_bytes()
                self.assertEqual(self.cache().snapshot(), {})
                self.assertEqual(self.path.read_bytes(), before)
        self.path.write_bytes(b"\xff")
        self.assertEqual(self.cache().snapshot(), {})

    def invalid_rows(self):
        valid = locations()[SERVICES[0]]
        for key, value in (("positions", []), ("positions", [valid["positions"][0]] * 13),
                           ("positions", {}), ("service", {"rid": SERVICES[0]}),
                           ("service", {"rid": SERVICES[0], "rtype": "light"}),
                           ("equalization_factor", True), ("equalization_factor", -0.1),
                           ("equalization_factor", 1.1), ("equalization_factor", float("nan")),
                           ("equalization_factor", float("inf")), ("extra", 0)):
            row = copy.deepcopy(valid)
            row[key] = value
            yield row
        for value in (True, False, -1.01, 1.01, float("nan"), float("inf"), "0", None):
            row = copy.deepcopy(valid)
            row["positions"][0]["x"] = value
            yield row
        for value in ({"x": 0, "y": 0}, {"x": 0, "y": 0, "z": 0, "extra": 1}):
            row = copy.deepcopy(valid)
            row["positions"] = [value]
            yield row
        row = copy.deepcopy(valid)
        row["service"]["extra"] = 0
        yield row

    def test_invalid_rows_fail_closed_on_load_and_raise_sanitized_on_merge(self):
        cache = self.cache()
        cache.merge(locations())
        original = cache.snapshot()
        for row in self.invalid_rows():
            with self.subTest(row=row):
                self.path.write_text(json.dumps(document({SERVICES[0]: row})))
                self.assertEqual(self.cache().snapshot(), {})
                with self.assertRaises(ValueError) as caught:
                    cache.merge({SERVICES[0]: row})
                self.assert_sanitized(caught.exception)
                self.assertEqual(cache.snapshot(), original)

    def test_invalid_mapping_does_not_mutate_memory_or_disk(self):
        cache = self.cache()
        cache.merge(locations())
        before = self.path.read_bytes()
        invalid = [None, [], {UNKNOWN: locations()[SERVICES[0]]},
                   {SERVICES[1]: locations()[SERVICES[0]]}]
        for observed in invalid:
            with self.assertRaises(ValueError) as caught:
                cache.merge(observed)
            self.assert_sanitized(caught.exception)
            self.assertEqual(cache.snapshot(), locations())
            self.assertEqual(self.path.read_bytes(), before)

    def test_before_rename_failure_leaves_original_bytes_and_memory_and_removes_temp(self):
        for operation in ("fsync", "replace", "write"):
            with self.subTest(operation=operation):
                cache = self.cache()
                cache.merge(locations())
                before = self.path.read_bytes()
                changed = locations()
                changed[SERVICES[0]]["positions"][0]["x"] = -1
                with patch(f"native_location_cache.os.{operation}",
                           side_effect=OSError(f"{self.path}: private-failure")):
                    with self.assertRaises(OSError) as caught:
                        cache.merge(changed)
                self.assert_sanitized(caught.exception)
                self.assertIsNone(caught.exception.__cause__)
                self.assertEqual(cache.snapshot(), locations())
                self.assertEqual(self.path.read_bytes(), before)
                self.assertEqual(list(self.path.parent.glob("tmp_native_locations_*")), [])

    def test_directory_fsync_follows_rename_and_precedes_memory_commit(self):
        cache = self.cache()
        cache.merge(locations())
        changed = locations()
        changed[SERVICES[0]]["equalization_factor"] = 1
        events = []
        directory_descriptors = []
        original_fsync = os.fsync
        original_replace = os.replace

        def synced(descriptor):
            self.assertEqual(cache._locations, locations())
            if stat.S_ISDIR(os.fstat(descriptor).st_mode):
                events.append("directory_fsync")
                directory_descriptors.append(descriptor)
                self.assertEqual(self.cache().snapshot(), changed)
            else:
                events.append("file_fsync")
            original_fsync(descriptor)

        def replaced(source, destination):
            self.assertEqual(cache._locations, locations())
            original_replace(source, destination)
            events.append("replace")

        with patch("native_location_cache.os.fsync", side_effect=synced), \
                patch("native_location_cache.os.replace", side_effect=replaced):
            cache.merge(changed)
        self.assertEqual(events, ["file_fsync", "replace", "directory_fsync"])
        self.assertEqual(cache.snapshot(), changed)
        self.assertEqual(len(directory_descriptors), 1)
        with self.assertRaises(OSError):
            os.fstat(directory_descriptors[0])

    def test_directory_sync_failure_keeps_memory_but_can_leave_new_valid_file(self):
        cache = self.cache()
        cache.merge(locations())
        changed = locations()
        changed[SERVICES[0]]["equalization_factor"] = 1
        directory_descriptors = []
        original_fsync = os.fsync

        def failed_directory_sync(descriptor):
            if stat.S_ISDIR(os.fstat(descriptor).st_mode):
                directory_descriptors.append(descriptor)
                raise OSError(f"{self.path}: private-failure")
            original_fsync(descriptor)

        with patch("native_location_cache.os.fsync", side_effect=failed_directory_sync):
            with self.assertRaises(OSError) as caught:
                cache.merge(changed)
        self.assert_sanitized(caught.exception)
        self.assertIsNone(caught.exception.__cause__)
        self.assertEqual(cache.snapshot(), locations())
        self.assertEqual(len(directory_descriptors), 1)
        with self.assertRaises(OSError):
            os.fstat(directory_descriptors[0])
        self.assertEqual(self.cache().snapshot(), changed)
        self.assertEqual(list(self.path.parent.glob("tmp_native_locations_*")), [])
        cache.merge(changed)
        self.assertEqual(cache.snapshot(), changed)

    def test_directory_open_failure_propagates_after_rename_without_memory_commit(self):
        cache = self.cache()
        cache.merge(locations())
        changed = locations()
        changed[SERVICES[0]]["equalization_factor"] = 1
        original_open = os.open

        def failed_directory_open(path, flags, *args, **kwargs):
            if Path(path) == self.path.parent:
                raise OSError(f"{self.path}: private-failure")
            return original_open(path, flags, *args, **kwargs)

        with patch("native_location_cache.os.open", side_effect=failed_directory_open):
            with self.assertRaises(OSError) as caught:
                cache.merge(changed)
        self.assert_sanitized(caught.exception)
        self.assertEqual(cache.snapshot(), locations())
        self.assertEqual(self.cache().snapshot(), changed)

    def test_private_file_and_created_directory_permissions(self):
        self.path = self.path.parent / "new" / "nested" / "locations.json"
        cache = self.cache()
        cache.merge(locations())
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.path.parent.parent.stat().st_mode), 0o700)
        self.path.chmod(0o644)
        changed = locations()
        changed[SERVICES[0]]["equalization_factor"] = 1
        cache.merge(changed)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_symlink_hardlink_directory_and_unowned_destinations_are_rejected(self):
        target = self.path.parent / "target"
        target.write_text(json.dumps(document()))
        self.path.symlink_to(target)
        self.assertEqual(self.cache().snapshot(), {})
        with self.assertRaises(OSError):
            self.cache().merge(locations())
        self.assertTrue(self.path.is_symlink())
        self.path.unlink()
        os.link(target, self.path)
        self.assertEqual(self.cache().snapshot(), {})
        with self.assertRaises(OSError):
            self.cache().merge(locations())
        self.assertEqual(self.path.stat().st_ino, target.stat().st_ino)
        self.path.unlink()
        self.path.mkdir()
        self.assertEqual(self.cache().snapshot(), {})
        with self.assertRaises(OSError):
            self.cache().merge(locations())
        self.path.rmdir()
        self.path.write_text(json.dumps(document()))
        with patch("native_location_cache.os.geteuid", return_value=os.geteuid() + 1):
            self.assertEqual(self.cache().snapshot(), {})
            with self.assertRaises(OSError):
                self.cache().merge(locations())
        self.assertEqual(target.read_text(), json.dumps(document()))

    def test_concurrent_updates_retain_all_services(self):
        cache = self.cache()
        rows = locations()
        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(lambda rid: cache.merge({rid: rows[rid]}), SERVICES))
        self.assertEqual(cache.snapshot(), rows)
        self.assertEqual(self.cache().snapshot(), rows)

    def test_replaced_destination_is_not_overwritten(self):
        cache = self.cache()
        original_fsync = os.fsync

        def destination_appears(descriptor):
            self.path.write_text("unrelated")
            original_fsync(descriptor)

        with patch("native_location_cache.os.fsync", side_effect=destination_appears):
            with self.assertRaises(OSError) as caught:
                cache.merge(locations())
        self.assert_sanitized(caught.exception)
        self.assertEqual(self.path.read_text(), "unrelated")
        self.assertEqual(cache.snapshot(), {})
        self.assertEqual(list(self.path.parent.glob("tmp_native_locations_*")), [])


if __name__ == "__main__":
    unittest.main()
