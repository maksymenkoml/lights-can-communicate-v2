"""Bounded explicit power requests delivered through the existing owner heartbeat."""

import math
import re
import threading
import uuid

from power_owner import GRANT_SECONDS, MAX_MESSAGE_AGE

REQUEST_SECONDS = 12
_MAX_REVISION = 2**53 - 1


class PowerRequestUnavailable(RuntimeError):
    """The owner cannot confirm a requested physical transition."""


def _id(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value) is not None


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def validate_capability(value, states):
    """Validate before renewing grants; old owners have no extension."""
    if value is None:
        return None
    if (type(value) is not dict or set(value) != {"version", "revisions", "results"}
            or type(value["version"]) is not int or value["version"] != 1
            or type(states) is not dict or type(value["revisions"]) is not dict
            or set(value["revisions"]) - set(states)
            or any(type(revision) is not int or not 0 <= revision <= _MAX_REVISION
                   for revision in value["revisions"].values())
            or type(value["results"]) is not list or len(value["results"]) > 64):
        raise ValueError("Invalid power request capability")
    seen = set()
    for result in value["results"]:
        if (type(result) is not dict or set(result) != {"id", "success"}
                or not _id(result["id"]) or result["id"] in seen
                or type(result["success"]) is not bool):
            raise ValueError("Invalid power request result")
        seen.add(result["id"])
    return value


class PowerRequests:
    def __init__(self, owner, observe=None):
        self.owner = owner
        # Fresh identity-verified native power per light, as sent to the owner.
        self.observe = observe
        self.clock, self.wall_clock = owner.clock, owner.wall_clock
        self.condition = threading.Condition()
        self.session = None
        self.capabilities = {}
        self.jobs = {}
        self.closed = False

    def _owner(self):
        with self.owner.lock:
            return self.owner.session, self.owner.snapshot()["lights"]

    def _expire(self):
        now = self.clock()
        session, lights = self._owner()
        for job in self.jobs.values():
            for command in job["commands"].values():
                if command["result"] is not None:
                    continue
                light = command["wire"]["light"]
                capability = self.capabilities.get(light)
                if (self.closed or job["session"] != session or job["session"] != self.session
                        or now >= job["deadline"] or not capability
                        or now >= capability[1] or lights[light]["grant_remaining"] <= 0):
                    command["result"] = False
        self.condition.notify_all()

    @staticmethod
    def _outcome(job):
        results = [command["result"] for command in job["commands"].values()]
        if False in results:
            return False
        return True if all(value is True for value in results) else None

    def exchange(self, envelope, capability, observed, accepted):
        if not accepted:
            return {}
        with self.condition, self.owner.lock:
            if (envelope["session"] != self.owner.session
                    or envelope["sequence"] != self.owner.sequence):
                return {}
            session = envelope["session"]
            if self.session != session:
                self.capabilities.clear()
                self.session = session
            until = self.clock() + GRANT_SECONDS - max(
                0, self.wall_clock() - envelope["issued_at"]
            )
            for light in envelope["states"]:
                if capability is not None and light in capability["revisions"]:
                    self.capabilities[light] = (capability["revisions"][light], until)
                else:
                    self.capabilities.pop(light, None)
            self._expire()
            if capability is None:
                return {}
            acknowledgements = []
            commands = {command["wire"]["id"]: command
                        for job in self.jobs.values() if job["session"] == session
                        for command in job["commands"].values()}
            for result in capability["results"]:
                acknowledgements.append(result["id"])
                command = commands.get(result["id"])
                if command is None or command["result"] is not None:
                    continue
                wire = command["wire"]
                light = wire["light"]
                proof = observed.get(light) if isinstance(observed, dict) else None
                current = self.capabilities.get(light)
                command["result"] = bool(
                    command["delivered"] and result["success"]
                    and current and current[0] == wire["revision"] + 1
                    and envelope["states"].get(light) is wire["on"]
                    and isinstance(proof, dict) and proof.get("on") is wire["on"]
                    and _number(proof.get("sample")) and proof["sample"] >= 0
                )
            outgoing = []
            for job in self.jobs.values():
                if self._outcome(job) is not None:
                    continue
                for command in job["commands"].values():
                    wire = command["wire"]
                    current = self.capabilities.get(wire["light"])
                    if command["result"] is not None or not current:
                        continue
                    allowed = {wire["revision"]}
                    if command["delivered"]:
                        allowed.add(wire["revision"] + 1)
                    if current[0] not in allowed:
                        command["result"] = False
                        continue
                    command["delivered"] = True
                    outgoing.append(dict(wire))
            self.condition.notify_all()
            return {"power_requests": outgoing, "power_request_acks": acknowledgements}

    def settled(self, states):
        """Whether Home already wants, and every lamp already has, the requested power.

        The Hue app repeats On with every scene and light change. Such a request
        changes nothing, so it needs no owner round trip, which takes a heartbeat
        or more. A light with a command in flight is never settled.
        """
        if self.observe is None:
            return False
        observed = self.observe()
        with self.condition:
            session, lights = self._owner()
            busy = {light for job in self.jobs.values()
                    for light, command in job["commands"].items() if command["result"] is None}
            return session is not None and session == self.session and all(
                light not in busy and lights[light]["grant_remaining"] > 0
                and lights[light]["requested_on"] is on
                and isinstance(observed.get(light), dict) and observed[light].get("on") is on
                for light, on in states.items())

    def request(self, payload):
        if (type(payload) is not dict or set(payload) != {"id", "issued_at", "states"}
                or not _id(payload["id"]) or not _number(payload["issued_at"])
                or abs(self.wall_clock() - payload["issued_at"]) > MAX_MESSAGE_AGE
                or type(payload["states"]) is not dict or not payload["states"]
                or set(payload["states"]) - set(self.owner.states)
                or any(type(value) is not bool for value in payload["states"].values())):
            raise ValueError("Invalid explicit power request")
        if payload["id"] not in self.jobs and self.settled(payload["states"]):
            return True
        with self.condition:
            now = self.clock()
            self._expire()
            job = self.jobs.get(payload["id"])
            if job is not None:
                if job["states"] != payload["states"]:
                    raise ValueError("Power request ID was reused")
                job["issued_at"] = max(job["issued_at"], payload["issued_at"])
            else:
                oldest = None
                if len(self.jobs) >= 64:
                    oldest = next((key for key, previous in self.jobs.items()
                                   if self.wall_clock() - previous["issued_at"] > MAX_MESSAGE_AGE
                                   and all(command["result"] is not None
                                          for command in previous["commands"].values())), None)
                session, lights = self._owner()
                busy = {light for existing in self.jobs.values()
                        for light, command in existing["commands"].items()
                        if command["result"] is None}
                if (self.closed or (len(self.jobs) >= 64 and oldest is None)
                        or session != self.session
                        or any(light in busy or light not in self.capabilities
                               or self.capabilities[light][1] <= now
                               or lights[light]["grant_remaining"] <= 0
                               for light in payload["states"])):
                    raise PowerRequestUnavailable("Power owner unavailable or busy")
                if oldest is not None:
                    del self.jobs[oldest]
                job = {"states": dict(payload["states"]), "session": session,
                       "issued_at": payload["issued_at"],
                       "deadline": now + REQUEST_SECONDS, "commands": {}}
                for light, on in payload["states"].items():
                    issued = self.wall_clock()
                    job["commands"][light] = {
                        "wire": {"id": uuid.uuid4().hex, "light": light, "on": on,
                                 "issued_at": issued,
                                 "expires_at": issued + REQUEST_SECONDS,
                                 "revision": self.capabilities[light][0]},
                        "delivered": False, "result": None,
                    }
                self.jobs[payload["id"]] = job
            while True:
                self._expire()
                outcome = self._outcome(job)
                if outcome is not None:
                    if not outcome:
                        raise PowerRequestUnavailable("Power request was not confirmed")
                    return True
                self.condition.wait(timeout=min(0.1, max(0, job["deadline"] - self.clock())))

    def close(self):
        with self.condition:
            self.closed = True
            self._expire()
