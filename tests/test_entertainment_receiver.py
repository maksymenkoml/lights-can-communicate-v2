import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "experiments" / "hue-bridge"))
from entertainment_receiver import StreamOutbox  # noqa: E402
from test_entertainment_stream import colors  # noqa: E402


class ReceiverTests(unittest.TestCase):
    def test_coalesces_frames_and_stop_replaces_unsent_frame(self):
        sent = []
        out = StreamOutbox("http://adapter", "test", sender=sent.append, clock=lambda: 1000)
        out.frame(colors())
        out.frame({k: (0, 0, 0) for k in colors()})
        self.assertTrue(out.forward_once())
        self.assertEqual(sent[0]["sequence"], 2)
        self.assertTrue(all(row[1:] == [0, 0, 0] for row in sent[0]["colors"]))
        out.frame(colors())
        out.stop()
        out.forward_once()
        self.assertEqual(sent[-1]["action"], "stop")
        self.assertNotIn("colors", sent[-1])

    def test_expired_frame_is_not_replayed_after_delivery_delay(self):
        now = [1000.0]
        sent = []
        out = StreamOutbox("http://adapter", "test", sender=sent.append, clock=lambda: now[0])
        out.frame(colors())
        now[0] += 1
        self.assertFalse(out.forward_once())
        self.assertEqual(sent, [])
