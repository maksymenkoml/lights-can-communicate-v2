import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "experiments" / "hue-bridge"))
from hue_events import EventJournal, install_event_journal  # noqa: E402


def data(frame):
    return json.loads(next(line[6:] for line in frame.splitlines() if line.startswith("data: ")))


class EventJournalTests(unittest.TestCase):
    def setUp(self):
        self.journal = EventJournal(limit=3, heartbeat=0.001, epoch="123")
        self.current = {"value": "initial"}
        self.snapshots = 0

    def snapshot(self):
        self.snapshots += 1
        return self.journal.position(), {"snapshot": dict(self.current)}

    def connect(self, last_id=None):
        stream = self.journal.stream(self.snapshot, last_id)
        self.assertEqual(next(stream), ": hi\n\n")
        return stream

    def test_new_connection_gets_current_state_then_only_new_events(self):
        stream = self.connect()
        self.assertEqual(data(next(stream)), [{"snapshot": {"value": "initial"}}])
        self.journal.publish({"value": "on"})
        frame = next(stream)
        self.assertIn("id: 123:1\n", frame)
        self.assertEqual(data(frame), [{"value": "on"}])

    def test_independent_readers_do_not_consume_or_clear_each_others_events(self):
        first, second = self.connect(), self.connect()
        next(first)
        next(second)
        self.journal.publish({"on": True})
        self.assertEqual(data(next(first)), [{"on": True}])
        self.assertEqual(data(next(second)), [{"on": True}])

    def test_retained_event_survives_legacy_broker_clear_while_reader_is_paused(self):
        legacy = []
        module = SimpleNamespace(StreamEvent=legacy.append)
        journal = install_event_journal(module, limit=3, heartbeat=0.001, epoch="456")
        stream = journal.stream(lambda: (journal.position(), {"snapshot": True}))
        next(stream)
        next(stream)
        module.StreamEvent({"value": "first"})
        self.assertEqual(data(next(stream)), [{"value": "first"}])
        module.StreamEvent({"value": "home-on"})
        legacy.clear()  # Actual upstream broker does this while SSE readers sleep.
        self.assertEqual(data(next(stream)), [{"value": "home-on"}])

    def test_messages_are_immutable_and_ids_never_repeat_after_legacy_batches(self):
        stream = self.connect()
        next(stream)
        value = {"on": {"on": False}}
        self.journal.publish(value)
        value["on"]["on"] = True
        first = next(stream)
        self.journal.publish({"on": {"on": True}})
        second = next(stream)
        self.assertEqual(data(first), [{"on": {"on": False}}])
        self.assertIn("id: 123:1\n", first)
        self.assertIn("id: 123:2\n", second)

    def test_valid_last_event_id_replays_remaining_history_without_snapshot(self):
        for value in ("a", "b", "c"):
            self.journal.publish({"value": value})
        stream = self.connect("123:1")
        self.assertEqual(data(next(stream)), [{"value": "b"}])
        self.assertEqual(data(next(stream)), [{"value": "c"}])
        self.assertEqual(self.snapshots, 0)

    def test_old_future_malformed_and_previous_boot_ids_resnapshot(self):
        for value in range(5):
            self.journal.publish({"value": value})
        for last_id in ("123:0", "123:99", "123:-1", "old:4", "bad", "123:1:2"):
            with self.subTest(last_id=last_id):
                stream = self.connect(last_id)
                frame = next(stream)
                self.assertIn("id: 123:5\n", frame)
                self.assertEqual(data(frame), [{"snapshot": {"value": "initial"}}])

    def test_slow_reader_with_history_gap_resnapshots_instead_of_missing_power(self):
        stream = self.connect()
        next(stream)
        for value in range(5):
            self.current["value"] = value
            self.journal.publish({"value": value})
        self.assertEqual(data(next(stream)), [{"snapshot": {"value": 4}}])
        self.assertEqual(self.snapshots, 2)

    def test_idle_stream_keeps_connection_alive_without_inventing_state(self):
        stream = self.connect()
        next(stream)
        self.assertEqual(next(stream), ": keepalive\n\n")
        self.assertEqual(self.journal.position(), 0)


if __name__ == "__main__":
    unittest.main()
