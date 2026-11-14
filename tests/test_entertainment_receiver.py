import io
import sys
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "experiments" / "hue-bridge"))
from entertainment_receiver import StreamOutbox, dtls_diagnostic  # noqa: E402
from test_entertainment_stream import colors  # noqa: E402


class ReceiverTests(unittest.TestCase):
    def test_rejected_http_response_is_closed_before_retrying_fresh_frame(self):
        response = io.BytesIO(b"temporarily unavailable")
        error = urllib.error.HTTPError("http://adapter/stream", 503, "Unavailable", {}, response)

        def unavailable(_):
            raise error

        out = StreamOutbox("http://adapter", "test", sender=unavailable, clock=lambda: 1000)
        out.frame(colors())
        self.assertFalse(out.forward_once())
        self.assertEqual(out.snapshot()["last_http_status"], 503)
        self.assertTrue(response.closed)

    def test_dtls_diagnostics_keep_only_static_categories_not_credentials(self):
        self.assertEqual(
            dtls_diagnostic(b"SSL_accept:SSLv3/TLS read client hello\n"), "client_hello"
        )
        self.assertEqual(
            dtls_diagnostic(b"SSL_accept:SSLv3/TLS read finished\n"), "handshake_finished"
        )
        self.assertEqual(
            dtls_diagnostic(b"error: bad record mac; private-key-material"), "authentication_error"
        )
        self.assertEqual(
            dtls_diagnostic(b"error: unknown detail private-key-material"), "other_ssl_error"
        )
        self.assertIsNone(dtls_diagnostic(b"identity: private-key-material"))

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
