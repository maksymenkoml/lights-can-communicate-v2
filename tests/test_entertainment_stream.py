import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "experiments" / "hue-bridge"))
from entertainment_stream import (  # noqa: E402
    AREA_ID,
    FrameBuffer,
    VideoLease,
    decode_frame,
    encode_frame,
    native_colors,
)


def colors():
    return {i: (65535, 32768, 0) for i in range(14)}


def envelope(action="frame", sequence=1, **values):
    return {
        "action": action,
        "session": "test-session",
        "sequence": sequence,
        "issued_at": 1000,
        **({"colors": [[k, *v] for k, v in colors().items()]} if action == "frame" else {}),
        **values,
    }


class StreamTests(unittest.TestCase):
    def test_v2_roundtrip_keeps_full_precision_and_selected_area(self):
        packet = encode_frame(AREA_ID, colors(), 254)
        self.assertEqual(packet[:16], b"HueStream\x02\x00\xfe\x00\x00\x00\x00")
        self.assertEqual(decode_frame(packet), colors())
        with self.assertRaises(ValueError):
            decode_frame(encode_frame("00000000-0000-0000-0000-000000000000", colors(), 1))

    def test_fragmented_pipe_and_coalesced_packets_preserve_boundaries(self):
        packet = encode_frame(AREA_ID, colors(), 1)
        buffer = FrameBuffer()
        self.assertEqual(buffer.feed(b"handshake banner\n" + packet[:40]), [])
        self.assertEqual(buffer.feed(packet[40:] + packet + packet), [packet, packet, packet])

    def test_rejects_truncation_duplicate_and_unknown_channels(self):
        packet = encode_frame(AREA_ID, colors(), 1)
        for malformed in (
            packet[:-1],
            packet + packet[52:59],
            packet[:52] + bytes([99]) + packet[53:],
        ):
            with self.subTest(length=len(malformed)), self.assertRaises(ValueError):
                decode_frame(malformed)

    def test_v1_rgb_maps_light_and_nine_gradient_segment_addresses(self):
        header = b"HueStream" + bytes([1, 0, 0, 0, 0, 0, 0])
        entries = [(0, 1), (0, 2)] + [(1, i) for i in range(9)] + [(0, 4), (0, 5), (0, 6)]
        packet = header + b"".join(
            struct.pack("!BHHHH", kind, ident, 65535, 0, 0) for kind, ident in entries
        )
        self.assertEqual(decode_frame(packet), {i: (65535, 0, 0) for i in range(14)})

    def test_native_map_matches_center_left_right_and_nine_strip_segments(self):
        frame = {i: (1000 + i, 2000 + i, 3000 + i) for i in range(14)}
        native = native_colors(frame)
        self.assertEqual(native[0], (506, 1006, 1506))
        self.assertEqual(native[1], (1011, 2011, 3011))
        self.assertEqual(native[2], (1013, 2013, 3013))
        self.assertEqual(native[3], (1002, 2002, 3002))
        self.assertEqual(native[11], (1010, 2010, 3010))

    def test_lease_expires_and_rejects_replays_old_sessions_and_foreign_stop(self):
        now = [10.0]
        lease = VideoLease(clock=lambda: now[0], wall_clock=lambda: 1000)
        self.assertTrue(lease.submit(envelope()))
        self.assertTrue(lease.active())
        self.assertFalse(lease.submit(envelope()))
        self.assertFalse(lease.submit(envelope("stop", session="foreign")))
        now[0] += 0.6
        self.assertFalse(lease.active())
        self.assertFalse(lease.submit(envelope()))
        self.assertTrue(lease.submit(envelope(session="new-session")))
        self.assertTrue(lease.submit(envelope("stop", sequence=2, session="new-session")))
        self.assertFalse(lease.active())

    def test_malformed_or_partial_frame_does_not_acquire_lease(self):
        lease = VideoLease(wall_clock=lambda: 1000)
        for value in (
            envelope(colors=[]),
            envelope(colors=[[0, 0, 0, True]]),
            envelope(issued_at=900),
            envelope(extra=1),
        ):
            with self.assertRaises(ValueError):
                lease.submit(value)
            self.assertFalse(lease.active())

    def test_shutdown_revokes_lease_and_rejects_late_frames(self):
        lease = VideoLease(wall_clock=lambda: 1000)
        lease.submit(envelope())
        lease.close()
        self.assertFalse(lease.active())
        self.assertFalse(lease.submit(envelope(sequence=2)))

    def test_fresh_higher_sequence_recovers_after_transport_gap(self):
        now = [10.0]
        lease = VideoLease(clock=lambda: now[0], wall_clock=lambda: 1000)
        lease.submit(envelope())
        now[0] += 0.6
        self.assertFalse(lease.active())
        self.assertFalse(lease.submit(envelope()))
        self.assertTrue(lease.submit(envelope(sequence=2)))
        self.assertTrue(lease.active())

    def test_parser_resynchronizes_after_bad_and_truncated_packets(self):
        packet = encode_frame(AREA_ID, colors(), 1)
        for malformed in (
            packet[:52] + bytes([255]) + packet[53:],
            packet[:10] + b"\x05" + packet[11:],
            packet[:9] + b"\x09" + packet[10:],
            packet[:80],
        ):
            with self.subTest(prefix=malformed[:16]):
                parser = FrameBuffer()
                self.assertEqual(parser.feed(malformed + packet), [packet])
                self.assertGreater(parser.rejected_frames, 0)
                self.assertLessEqual(len(parser.buffer), 8)

    def test_long_garbage_and_split_magic_do_not_lose_next_packet(self):
        parser = FrameBuffer()
        packet = encode_frame(AREA_ID, colors(), 1)
        self.assertEqual(parser.feed(b"x" * 100000 + packet[:5]), [])
        self.assertLessEqual(len(parser.buffer), 8)
        self.assertEqual(parser.feed(packet[5:]), [packet])

    def test_retired_source_stop_cannot_release_new_source(self):
        now = [10.0]
        lease = VideoLease(clock=lambda: now[0], wall_clock=lambda: 1000)
        self.assertTrue(lease.submit(envelope()))
        now[0] += 0.6
        self.assertTrue(lease.submit(envelope(session="replacement")))
        self.assertFalse(lease.submit(envelope("stop", sequence=999)))
        self.assertEqual(lease.latest()[0], "replacement")

    def test_new_host_accepts_current_source_sequence_without_replaying_old_queue(self):
        lease = VideoLease(wall_clock=lambda: 1000)
        self.assertTrue(lease.submit(envelope(sequence=123456)))
        self.assertFalse(lease.submit(envelope(sequence=123455)))
        self.assertEqual(lease.latest()[1], 123456)
