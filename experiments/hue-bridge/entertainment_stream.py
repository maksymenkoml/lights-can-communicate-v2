"""Pinned desk HueStream framing and short-lived video ownership."""

import copy
import math
import struct
import threading
import time
import uuid
from collections import deque

MAGIC = b"HueStream"
GROUP_ID = str(uuid.uuid5(uuid.NAMESPACE_URL, "smart-lighting-probe/DeskEntertainment"))
AREA_ID = str(uuid.uuid5(uuid.NAMESPACE_URL, GROUP_ID + "entertainment_configuration"))
NATIVE_AREA_ID = "10c81079-56a6-45c4-8149-55185f703083"
CHANNELS = set(range(14))
SLOT_CHANNELS = {"1": [0], "2": [1], "3": list(range(2, 11)), "4": [11], "5": [12], "6": [13]}
STRIP_POSITIONS = [
    (-0.4, 0.8, -0.4),
    (-0.4, 0.8, 0),
    (-0.4, 0.8, 0.4),
    (-0.22, 0.8, 0.4),
    (0, 0.8, 0.4),
    (0.22, 0.8, 0.4),
    (0.4, 0.8, 0.4),
    (0.4, 0.8, 0),
    (0.4, 0.8, -0.4),
]
POSITIONS = [
    (-0.7, 0.8, 0),
    (0.7, 0.8, 0),
    *STRIP_POSITIONS,
    (-0.62976, 1, -1),
    (-0.09803, 1, -1),
    (0.43061, 1, -1),
]


def validate_colors(rows):
    if not isinstance(rows, list) or len(rows) != 14:
        raise ValueError("Expected fourteen desk channels")
    result = {}
    for row in rows:
        if (
            not isinstance(row, (list, tuple))
            or len(row) != 4
            or any(type(x) is not int for x in row)
        ):
            raise ValueError("Invalid channel color")
        channel, *rgb = row
        if channel not in CHANNELS or channel in result or any(not 0 <= v <= 65535 for v in rgb):
            raise ValueError("Unexpected or duplicate channel")
        result[channel] = tuple(rgb)
    return result


def encode_frame(area, colors, sequence):
    if not colors or any(type(k) is not int or not 0 <= k <= 255 for k in colors):
        raise ValueError("Invalid output channels")
    prefix = (
        MAGIC + bytes([2, 0, sequence % 256, 0, 0, 0, 0]) + str(uuid.UUID(area)).encode("ascii")
    )
    return prefix + b"".join(
        struct.pack("!BHHH", channel, *rgb) for channel, rgb in sorted(colors.items())
    )


def decode_frame(data):
    if len(data) < 16 or data[:9] != MAGIC or data[10] != 0 or data[14] not in (0, 1):
        raise ValueError("Invalid HueStream header")
    if data[9] == 2:
        if len(data) != 52 + 14 * 7 or data[16:52] != AREA_ID.encode():
            raise ValueError("Unexpected area or frame size")
        rows = [list(struct.unpack("!BHHH", data[i : i + 7])) for i in range(52, len(data), 7)]
    elif data[9] == 1:
        if len(data) != 16 + 14 * 9:
            raise ValueError("Unexpected v1 frame size")
        rows = []
        single = {1: 0, 2: 1, 4: 11, 5: 12, 6: 13}
        for i in range(16, len(data), 9):
            kind, ident, r, g, b = struct.unpack("!BHHHH", data[i : i + 9])
            channel = (
                ident + 2
                if kind == 1 and 0 <= ident < 9
                else single.get(ident)
                if kind == 0
                else None
            )
            if channel is None:
                raise ValueError("Unexpected v1 light")
            rows.append([channel, r, g, b])
    else:
        raise ValueError("Unsupported HueStream version")
    result = validate_colors(rows)
    if data[14] == 1:
        # HueStream xyY uses 16-bit normalized chromaticity and intensity.
        converted = {}
        for channel, (x, y, level) in result.items():
            if not level:
                converted[channel] = (0, 0, 0)
                continue
            if not y or x + y > 65536:
                raise ValueError("Invalid xyY stream color")
            x, y = x / 65535, y / 65535
            X, Y, Z = x / y, 1, (1 - x - y) / y
            rgb = [
                max(0, X * 1.656492 - Y * 0.354851 - Z * 0.255038),
                max(0, -X * 0.707196 + Y * 1.655397 + Z * 0.036152),
                max(0, X * 0.051713 - Y * 0.121364 + Z * 1.011530),
            ]
            rgb = [12.92 * v if v <= 0.0031308 else 1.055 * v ** (1 / 2.4) - 0.055 for v in rgb]
            top = max(1, *rgb)
            converted[channel] = tuple(round(v / top * level) for v in rgb)
        result = converted
    return result


def native_colors(frame):
    mapping = {0: 12, 1: 11, 2: 13, **{i + 3: i + 2 for i in range(9)}}
    return {
        channel: tuple(round(value * (0.5 if channel == 0 else 1)) for value in frame[source])
        for channel, source in mapping.items()
    }


class FrameBuffer:
    def __init__(self):
        self.buffer = b""

    def feed(self, chunk):
        self.buffer += chunk
        frames = []
        while True:
            start = self.buffer.find(MAGIC)
            if start < 0:
                self.buffer = self.buffer[-8:]
                break
            self.buffer = self.buffer[start:]
            if len(self.buffer) < 16:
                break
            size = 52 + 14 * 7 if self.buffer[9] == 2 else 16 + 14 * 9 if self.buffer[9] == 1 else 0
            if not size:
                self.buffer = self.buffer[9:]
                continue
            if len(self.buffer) < size:
                break
            frame, self.buffer = self.buffer[:size], self.buffer[size:]
            decode_frame(frame)
            frames.append(frame)
        return frames


class VideoLease:
    def __init__(self, clock=time.monotonic, wall_clock=time.time):
        self.clock, self.wall_clock = clock, wall_clock
        self.lock = threading.RLock()
        self.session = None
        self.sequence = -1
        self.seen = float("-inf")
        self.issued = float("-inf")
        self.closed = deque(maxlen=64)
        self.frame = None
        self.received = 0
        self.stopping = False

    def close(self):
        with self.lock:
            self.stopping = True
            self.session, self.frame = None, None

    def active(self):
        with self.lock:
            if self.session and self.clock() - self.seen > 0.5:
                self.closed.append(self.session)
                self.session, self.frame = None, None
            return self.session is not None

    def submit(self, message):
        if not isinstance(message, dict) or message.get("action") not in ("frame", "stop"):
            raise ValueError("Invalid stream action")
        expected = {"action", "session", "sequence", "issued_at"} | (
            {"colors"} if message["action"] == "frame" else set()
        )
        if set(message) != expected:
            raise ValueError("Invalid stream envelope")
        session, sequence, issued = message["session"], message["sequence"], message["issued_at"]
        if (
            not isinstance(session, str)
            or not 1 <= len(session) <= 80
            or type(sequence) is not int
            or not 0 <= sequence < 2**63
        ):
            raise ValueError("Invalid stream ordering")
        if (
            type(issued) not in (int, float)
            or not math.isfinite(issued)
            or abs(self.wall_clock() - issued) > 0.5
        ):
            raise ValueError("Expired frame")
        frame = validate_colors(message["colors"]) if message["action"] == "frame" else None
        with self.lock:
            self.active()
            if self.stopping:
                return False
            if (
                session in self.closed
                or (self.session and session != self.session)
                or (session == self.session and sequence <= self.sequence)
                or (not self.session and issued < self.issued)
            ):
                return False
            if frame is None:
                if session != self.session:
                    return False
                self.closed.append(session)
                self.session, self.frame = None, None
                return True
            self.session, self.sequence, self.issued, self.seen = (
                session,
                sequence,
                issued,
                self.clock(),
            )
            self.frame = frame
            self.received += 1
            return True

    def latest(self):
        with self.lock:
            return (
                (self.session, self.sequence, copy.deepcopy(self.frame)) if self.active() else None
            )

    def snapshot(self):
        with self.lock:
            return {
                "active": self.active(),
                "frames_received": self.received,
                "sequence": self.sequence,
                "frame_age": self.clock() - self.seen if math.isfinite(self.seen) else None,
            }
