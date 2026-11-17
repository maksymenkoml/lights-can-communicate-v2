"""Bounded explicit power requests delivered through the existing owner heartbeat."""

import math
import re
import threading
import uuid

from power_owner import GRANT_SECONDS, MAX_MESSAGE_AGE

REQUEST_SECONDS = 12
_ROLES = {"left", "right"}
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
            or set(value["revisions"]) - (set(states) & _ROLES)
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
    def __init__(self, owner):
        self.owner = owner
        self.clock, self.wall_clock = owner.clock, owner.wall_clock
        self.condition = threading.Condition()
        self.session = None
        self.capabilities = {}
        self.jobs = {}
        self.closed = False

    def _owner(self):
        with self.owner.lock:
            return self.owner.session, self.owner.snapshot()["roles"]

    def _expire(self):
        now = self.clock()
        session, roles = self._owner()
        for job in self.jobs.values():
            for command in job["commands"].values():
                if command["result"] is not None:
                    continue
                role = command["wire"]["role"]
                capability = self.capabilities.get(role)
                if (self.closed or job["session"] != session or job["session"] != self.session
                        or now >= job["deadline"] or not capability
                        or now >= capability[1] or roles[role]["grant_remaining"] <= 0):
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
            for role in envelope["states"]:
                if capability is not None and role in capability["revisions"]:
                    self.capabilities[role] = (capability["revisions"][role], until)
                else:
                    self.capabilities.pop(role, None)
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
                role = wire["role"]
                proof = observed.get(role) if isinstance(observed, dict) else None
                current = self.capabilities.get(role)
                command["result"] = bool(
                    command["delivered"] and result["success"]
                    and current and current[0] == wire["revision"] + 1
                    and envelope["states"].get(role) is wire["on"]
                    and isinstance(proof, dict) and proof.get("on") is wire["on"]
                    and _number(proof.get("sample")) and proof["sample"] >= 0
                )
            outgoing = []
            for job in self.jobs.values():
                if self._outcome(job) is not None:
                    continue
                for command in job["commands"].values():
                    wire = command["wire"]
                    current = self.capabilities.get(wire["role"])
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

    def request(self, payload):
        if (type(payload) is not dict or set(payload) != {"id", "issued_at", "states"}
                or not _id(payload["id"]) or not _number(payload["issued_at"])
                or abs(self.wall_clock() - payload["issued_at"]) > MAX_MESSAGE_AGE
                or type(payload["states"]) is not dict or not payload["states"]
                or set(payload["states"]) - _ROLES
                or any(type(value) is not bool for value in payload["states"].values())):
            raise ValueError("Invalid explicit power request")
        with self.condition:
            now = self.clock()
            self._expire()
            job = self.jobs.get(payload["id"])
            if job is not None:
                if job["states"] != payload["states"]:
                    raise ValueError("Power request ID was reused")
            else:
                oldest = None
                if len(self.jobs) >= 64:
                    oldest = next((key for key, previous in self.jobs.items()
                                   if all(command["result"] is not None
                                          for command in previous["commands"].values())), None)
                session, roles = self._owner()
                busy = {role for existing in self.jobs.values()
                        for role, command in existing["commands"].items()
                        if command["result"] is None}
                if (self.closed or (len(self.jobs) >= 64 and oldest is None)
                        or session != self.session
                        or any(role in busy or role not in self.capabilities
                               or self.capabilities[role][1] <= now
                               or roles[role]["grant_remaining"] <= 0
                               for role in payload["states"])):
                    raise PowerRequestUnavailable("Power owner unavailable or busy")
                if oldest is not None:
                    del self.jobs[oldest]
                job = {"states": dict(payload["states"]), "session": session,
                       "deadline": now + REQUEST_SECONDS, "commands": {}}
                for role, on in payload["states"].items():
                    issued = self.wall_clock()
                    job["commands"][role] = {
                        "wire": {"id": uuid.uuid4().hex, "role": role, "on": on,
                                 "issued_at": issued,
                                 "expires_at": issued + REQUEST_SECONDS,
                                 "revision": self.capabilities[role][0]},
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
