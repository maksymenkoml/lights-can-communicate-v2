"""Bounded non-destructive SSE fanout alongside the pinned legacy event queue."""

import importlib
import json
import threading
import time
from collections import deque


class EventJournal:
    def __init__(self, limit=512, heartbeat=15, epoch=None):
        self.rows = deque(maxlen=limit)
        self.condition = threading.Condition()
        self.sequence = 0
        self.epoch = str(time.time_ns()) if epoch is None else epoch
        self.heartbeat = heartbeat

    def publish(self, message):
        with self.condition:
            encoded = json.dumps([message], separators=(",", ":"))
            self.sequence += 1
            self.rows.append((self.sequence, encoded))
            self.condition.notify_all()

    def position(self):
        with self.condition:
            return self.sequence

    def retained(self, cursor):
        return (cursor is not None and 0 <= cursor <= self.sequence
                and (not self.rows or cursor >= self.rows[0][0] - 1))

    def frame(self, sequence, encoded):
        return f"id: {self.epoch}:{sequence}\ndata: {encoded}\n\n"

    def stream(self, snapshot, last_id=None):
        """snapshot returns (journal cursor, message) under the model's writer lock."""
        cursor = None
        if isinstance(last_id, str):
            parts = last_id.split(":")
            if len(parts) == 2 and parts[0] == self.epoch and parts[1].isdigit():
                cursor = int(parts[1])
        yield ": hi\n\n"
        while True:
            with self.condition:
                gap = not self.retained(cursor)
                batch = [] if gap else [row for row in self.rows if row[0] > cursor]
                if not gap and not batch:
                    self.condition.wait(timeout=self.heartbeat)
                    gap = not self.retained(cursor)
                    batch = [] if gap else [row for row in self.rows if row[0] > cursor]
            if gap:
                # Never acquire the model lock while holding the journal lock:
                # publishers hold the model lock before entering publish().
                cursor, message = snapshot()
                yield self.frame(cursor, json.dumps([message], separators=(",", ":")))
            elif batch:
                for sequence, encoded in batch:
                    cursor = sequence
                    yield self.frame(sequence, encoded)
            else:
                yield ": keepalive\n\n"


def install_event_journal(module=None, **options):
    module = module or importlib.import_module("HueObjects")
    journal = EventJournal(**options)
    original = module.StreamEvent

    def publish(message):
        journal.publish(message)
        return original(message)

    module.StreamEvent = publish
    return journal


def install_event_route(app, journal, snapshot, authorize):
    from flask import Response, abort, request, stream_with_context

    endpoint = "stream.streamV2Events"
    if endpoint not in app.view_functions:
        raise RuntimeError("Pinned Hue event route is unavailable")

    def stream_events():
        if not authorize(request.headers):
            abort(403)
        return Response(
            stream_with_context(journal.stream(snapshot, request.headers.get("Last-Event-ID"))),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    app.view_functions[endpoint] = stream_events
