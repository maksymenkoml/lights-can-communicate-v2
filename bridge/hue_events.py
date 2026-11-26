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

    def _after(self, cursor):
        """The retained events after ``cursor``; a reader too slow for the journal
        skips the events it lost and gets the oldest ones still kept."""
        return [row for row in self.rows if row[0] > cursor]

    def frame(self, sequence, encoded):
        return f"id: {self.epoch}:{sequence}\ndata: {encoded}\n\n"

    def stream(self, last_id=None):
        """Yield the events after ``last_id``, then each new one, like a Hue bridge.

        A subscriber reads the state over REST and then follows the events. One that
        resumes with an ``id`` still in the journal gets the events it missed; any
        other (new, older than the journal, from a previous boot) carries on from
        now. It never gets a full state snapshot: tens of kilobytes on each
        reconnect make the Hue Sync Box drop the stream, treat the bridge as lost
        and restart its Entertainment session, which ends Sync (#70).
        """
        cursor = None
        if isinstance(last_id, str):
            parts = last_id.split(":")
            if len(parts) == 2 and parts[0] == self.epoch and parts[1].isdigit():
                cursor = int(parts[1])
        with self.condition:
            if not self.retained(cursor):
                cursor = self.sequence
        yield ": hi\n\n"
        while True:
            with self.condition:
                batch = self._after(cursor)
                if not batch:
                    self.condition.wait(timeout=self.heartbeat)
                    batch = self._after(cursor)
            if batch:
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


def install_event_route(app, journal, authorize):
    from flask import Response, abort, request, stream_with_context

    endpoint = "stream.streamV2Events"
    if endpoint not in app.view_functions:
        raise RuntimeError("Pinned Hue event route is unavailable")

    def stream_events():
        if not authorize(request.headers):
            abort(403)
        return Response(
            stream_with_context(journal.stream(request.headers.get("Last-Event-ID"))),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    app.view_functions[endpoint] = stream_events
