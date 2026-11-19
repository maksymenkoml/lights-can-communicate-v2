import json
import math
import os
import stat
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge"))
from stream_diagnostics import StreamDiagnostics  # noqa: E402


class StreamDiagnosticsTests(unittest.TestCase):
    def test_deduplicates_state_and_reason_even_when_metrics_change(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            diagnostics = StreamDiagnostics(path, clock=lambda: 12, wall_clock=lambda: 0)
            diagnostics.record("streaming", "frames_received", {"frames_received": 1})
            for count in range(2, 100):
                diagnostics.record("streaming", "frames_received", {"frames_received": count})
            diagnostics.record("waiting", "transport_error", {"errno": 32})
            diagnostics.record("streaming", "frames_received", {"frames_received": 100})
            snapshot = diagnostics.snapshot()
            self.assertEqual(len(snapshot["events"]), 3)
            self.assertEqual(snapshot["events_written"], 3)
            self.assertEqual(len(path.read_text().splitlines()), 3)
            self.assertEqual(snapshot["events"][0]["metrics"], {"frames_received": 1})
            self.assertEqual(snapshot["events"][0]["timestamp"], "1970-01-01T00:00:00Z")

    def test_rejects_invalid_categories_and_secret_metrics_on_both_outputs(self):
        secret = "secret-token@192.0.2.10/private.key"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            diagnostics = StreamDiagnostics(path)
            diagnostics.record(secret, "frames_received", {"frames_received": 1})
            diagnostics.record("streaming", secret, {"frames_received": 1})
            diagnostics.record([], {}, {"frames_received": 1})
            diagnostics.record(
                "streaming",
                "frames_received",
                {
                    "frames_received": 4,
                    "frames_sent": None,
                    "forwarded": 2.5,
                    "failed": True,
                    "frame_age": float("nan"),
                    "duration_ms": float("inf"),
                    "consecutive_failures": float("-inf"),
                    "errno": secret,
                    "http_status": {"authorization": secret},
                    secret: 99,
                    "address": secret,
                },
            )
            snapshot = diagnostics.snapshot()
            self.assertEqual(len(snapshot["events"]), 1)
            self.assertEqual(
                snapshot["events"][0]["metrics"],
                {"frames_received": 4, "frames_sent": None, "forwarded": 2.5},
            )
            self.assertNotIn(secret, json.dumps(snapshot))
            self.assertNotIn(secret, path.read_text())
            self.assertEqual(snapshot["write_errors"], 0)

    def test_accepts_all_documented_numeric_metric_fields(self):
        metrics = {
            "frames_received": 1,
            "frames_sent": 2,
            "forwarded": 3,
            "failed": 4,
            "frame_age": 0.5,
            "duration_ms": 2.5,
            "consecutive_failures": 0,
            "errno": 32,
            "http_status": None,
        }
        diagnostics = StreamDiagnostics()
        diagnostics.record("waiting", "transport_error", metrics)
        self.assertEqual(diagnostics.snapshot()["events"][0]["metrics"], metrics)
        self.assertEqual(diagnostics.snapshot()["events_written"], 0)

    def test_ring_retains_latest_64_and_snapshots_are_independent(self):
        diagnostics = StreamDiagnostics()
        for count in range(100):
            state = "streaming" if count % 2 else "waiting"
            diagnostics.record(state, "frames_received", {"frames_received": count})
        snapshot = diagnostics.snapshot()
        self.assertEqual(len(snapshot["events"]), 64)
        self.assertEqual(snapshot["events"][0]["metrics"]["frames_received"], 36)
        snapshot["events"][0]["metrics"]["frames_received"] = -1
        snapshot["events"].clear()
        self.assertEqual(
            diagnostics.snapshot()["events"][0]["metrics"]["frames_received"], 36
        )

    def test_elapsed_uses_monotonic_clock_and_timestamp_uses_utc(self):
        now = [10.0]
        diagnostics = StreamDiagnostics(clock=lambda: now[0], wall_clock=lambda: 1_700_000_000)
        now[0] = 12.5
        diagnostics.record("idle", "starting")
        event = diagnostics.snapshot()["events"][0]
        self.assertEqual(event["elapsed"], 2.5)
        self.assertEqual(event["timestamp"], "2023-11-14T22:13:20Z")

    def test_broken_clocks_and_malformed_metrics_do_not_escape(self):
        def broken_clock():
            raise ValueError("secret-clock-error")

        diagnostics = StreamDiagnostics(clock=broken_clock, wall_clock=broken_clock)
        diagnostics.record("idle", "starting", object())
        event = diagnostics.snapshot()["events"][0]
        self.assertNotIn("elapsed", event)
        self.assertEqual(event["metrics"], {})
        self.assertNotIn("secret-clock-error", json.dumps(diagnostics.snapshot()))

    def test_rotation_is_bounded_private_and_preserves_unrelated_files(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            unrelated = Path(directory) / "events.jsonl.2"
            unrelated.write_text("keep")
            diagnostics = StreamDiagnostics(path, max_bytes=600)
            for count in range(50):
                state = "streaming" if count % 2 else "waiting"
                diagnostics.record(state, "frames_received", {"frames_received": count})
            self.assertEqual(diagnostics.snapshot()["events_written"], 50)
            self.assertEqual(diagnostics.snapshot()["write_errors"], 0)
            for journal in (path, Path(str(path) + ".1")):
                self.assertTrue(journal.exists())
                self.assertLessEqual(journal.stat().st_size, 600)
                self.assertEqual(stat.S_IMODE(journal.stat().st_mode), 0o600)
                for line in journal.read_text().splitlines():
                    self.assertIn(json.loads(line)["state"], {"streaming", "waiting"})
            self.assertEqual(unrelated.read_text(), "keep")

    def test_existing_journal_permissions_become_private(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            path.write_text("")
            path.chmod(0o644)
            diagnostics = StreamDiagnostics(path)
            diagnostics.record("idle", "starting")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_disk_failure_preserves_ram_counts_and_deduplication(self):
        with tempfile.TemporaryDirectory() as directory:
            diagnostics = StreamDiagnostics(Path(directory) / "missing" / "events.jsonl")
            diagnostics.record("waiting", "transport_error")
            diagnostics.record("waiting", "transport_error")
            diagnostics.record("streaming", "transport_recovered")
            snapshot = diagnostics.snapshot()
            self.assertEqual(len(snapshot["events"]), 2)
            self.assertEqual(snapshot["events_written"], 0)
            self.assertEqual(snapshot["write_errors"], 2)

    def test_oversized_event_never_exceeds_disk_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            diagnostics = StreamDiagnostics(path, max_bytes=1)
            diagnostics.record("idle", "starting")
            snapshot = diagnostics.snapshot()
            self.assertEqual(len(snapshot["events"]), 1)
            self.assertEqual(snapshot["events_written"], 0)
            self.assertEqual(snapshot["write_errors"], 1)
            self.assertFalse(path.exists())

    def test_partial_write_failure_does_not_corrupt_later_jsonl_records(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            diagnostics = StreamDiagnostics(path)
            diagnostics.record("idle", "starting")
            original_write = os.write
            writes = [0]

            def partial_then_failure(descriptor, data):
                writes[0] += 1
                if writes[0] == 1:
                    return original_write(descriptor, data[:10])
                raise OSError("secret-disk-error")

            with patch("stream_diagnostics.os.write", side_effect=partial_then_failure):
                diagnostics.record("waiting", "transport_error")
            diagnostics.record("streaming", "transport_recovered")
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual([row["state"] for row in rows], ["idle", "streaming"])
            snapshot = diagnostics.snapshot()
            self.assertEqual(len(snapshot["events"]), 3)
            self.assertEqual(snapshot["events_written"], 2)
            self.assertEqual(snapshot["write_errors"], 1)
            self.assertNotIn("secret-disk-error", json.dumps(snapshot))

    def test_symlink_destinations_are_not_followed(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "private-state"
            target.write_text("untouched")
            path = Path(directory) / "events.jsonl"
            path.symlink_to(target)
            diagnostics = StreamDiagnostics(path)
            diagnostics.record("idle", "starting")
            self.assertEqual(target.read_text(), "untouched")
            self.assertEqual(diagnostics.snapshot()["write_errors"], 1)

    def test_symlink_backup_does_not_modify_its_target(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            target = Path(directory) / "private-state"
            target.write_text("untouched")
            Path(str(path) + ".1").symlink_to(target)
            diagnostics = StreamDiagnostics(path, max_bytes=300)
            for count in range(6):
                diagnostics.record("idle" if count % 2 else "waiting", "starting")
            self.assertEqual(target.read_text(), "untouched")
            self.assertGreater(diagnostics.snapshot()["write_errors"], 0)

    def test_parallel_records_and_snapshots_remain_valid(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            diagnostics = StreamDiagnostics(path, max_bytes=1_000_000)

            def record_batch(worker):
                for count in range(50):
                    diagnostics.record(
                        "idle" if count % 2 else "waiting",
                        "starting",
                        {"frames_received": worker * 50 + count},
                    )
                    snapshot = diagnostics.snapshot()
                    self.assertLessEqual(len(snapshot["events"]), 64)

            with ThreadPoolExecutor(max_workers=8) as executor:
                list(executor.map(record_batch, range(8)))
            snapshot = diagnostics.snapshot()
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(len(rows), snapshot["events_written"])
            self.assertEqual(snapshot["write_errors"], 0)
            self.assertEqual(snapshot["events"], rows[-64:])
            self.assertTrue(all(math.isfinite(row["elapsed"]) for row in rows))
