"""Local HomeKit power authority: the adapter is the only writer to both Govee.

It replaces the remote owner heartbeat when the Homebridge plugin owns the
accessories. HomeKit sends intent; this module executes power itself and keeps
the color permission closed until native On is confirmed. It exposes the same
gate interface as ``PowerOwner`` (``allowed``, ``revision``, ``wrap``,
``snapshot``) so every color writer stays unchanged.
"""

import colorsys
import hashlib
import hmac
import json
import math
import os
import re
import threading
import time

from power_history import PowerHistory
from power_owner import GRANT_SECONDS, MAX_MESSAGE_AGE, switch_power
from power_requests import PowerRequestUnavailable

import lights_can_communicate_v2 as lan

ROLES = ("left", "right")
CONFIRM_SECONDS = 3.0
RESEND_SECONDS = 1.0
SETTLE_SECONDS = 3.0
ADOPT_SECONDS = 2.0
_FIELDS = {"on", "brightness", "hue", "saturation", "color_temperature"}


def _number(value, low, high):
    return (type(value) in (int, float) and math.isfinite(value) and low <= value <= high)


def _empty():
    return {"on": None, "brightness": None, "hue": None, "saturation": None, "kelvin": None}


class HomeKitPower:
    def __init__(self, targets, state_file, sender, observe, idle=lambda side: True,
                 clock=time.monotonic, sleep=time.sleep, names=dict, wall_clock=time.time,
                 release=lambda side: None):
        self.roles = {lan.device_key(device): device["side"] for device in targets}
        if len(self.roles) != 2 or set(self.roles.values()) != set(ROLES):
            raise ValueError("HomeKit power requires the two selected roles")
        self.devices = {device["side"]: device for device in targets}
        self.state_file = state_file
        self.sender, self.observe, self.idle = sender, observe, idle
        # Ends a Hue scene on a lamp so that an explicit Home brightness or
        # color takes over; it leaves Sync output alone.
        self.release = release
        self.clock, self.sleep = clock, sleep
        # Owner names of the lamps from Light Studio: {role: name}, possibly empty.
        self.names = names
        self.lock = threading.RLock()
        self.transitions = {side: threading.Lock() for side in ROLES}
        self.intent = self._load()
        self.confirmed = {side: False for side in ROLES}
        # A power command that native state has not confirmed (yet).
        self.unconfirmed = {side: False for side in ROLES}
        self.revisions = {side: 0 for side in ROLES}
        self.last_command = {side: float("-inf") for side in ROLES}
        self.disagree_since = {side: None for side in ROLES}
        # Sync holds (sync_off.py): lamps switched off for a Sync session. A hold
        # is not HomeKit intent, is never adopted by tick() and closes the gate.
        self.holds = set()
        self.history = PowerHistory(clock, wall_clock)

    def _changed(self, side, cause):
        """Advance the gate revision and record it; the caller holds the lock."""
        self.revisions[side] += 1
        self.history.record(side, cause, self.intent[side]["on"], self.allowed(side),
                            side in self.holds)

    # Persistence ----------------------------------------------------------

    def _load(self):
        result = {side: _empty() for side in ROLES}
        try:
            with open(self.state_file, "rb") as stream:
                data = json.loads(stream.read(65536))
            roles = data["roles"] if data.get("version") == 1 else {}
            for side in ROLES:
                saved = roles.get(side, {})
                if type(saved.get("on")) is bool:
                    result[side]["on"] = saved["on"]
                for key, low, high in (("brightness", 1, 100), ("hue", 0, 360),
                                       ("saturation", 0, 100), ("kelvin", 2000, 9000)):
                    if _number(saved.get(key), low, high):
                        result[side][key] = saved[key]
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            # Unknown intent is adopted from the next fresh native reading.
            pass
        return result

    def _save(self):
        data = (json.dumps({"version": 1, "roles": self.intent}, sort_keys=True) + "\n").encode()
        temporary = f"{self.state_file}.{os.getpid()}.tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
            os.replace(temporary, self.state_file)
        except OSError:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    # PowerOwner-compatible gate -------------------------------------------

    def revision(self, side):
        with self.lock:
            return self.revisions.get(side, 0)

    def allowed(self, side):
        with self.lock:
            return (self.intent.get(side, {}).get("on") is True and side not in self.holds
                    and self.confirmed.get(side, False))

    def settled(self, side, on):
        """Whether Home already wants, and the lamp already has, this power state."""
        observed = self.observe().get(side, {})
        with self.lock:
            return (self.intent[side]["on"] is on and not self.unconfirmed[side]
                    and (self.confirmed[side] or not on) and observed.get("on") is on)

    def wrap(self, sender):
        def send(device, command, data):
            # Closing the gate takes this lock, so it waits for an in-flight send.
            with self.lock:
                if not self.allowed(self.roles.get(lan.device_key(device))):
                    return False
                return sender(device, command, data)

        return send

    def snapshot(self):
        with self.lock:
            return {
                "enforced": True,
                "authority": "homekit",
                "events": self.history.snapshot(),
                "roles": {
                    side: {"requested_on": self.intent[side]["on"] is True,
                           "color_allowed": self.allowed(side),
                           "revision": self.revisions[side],
                           "sync_hold": side in self.holds,
                           # A local authority never expires; unknown intent is not published.
                           "grant_remaining": GRANT_SECONDS
                           if self.intent[side]["on"] is not None else 0}
                    for side in ROLES
                },
            }

    # HomeKit state and intent ---------------------------------------------

    def state(self):
        observed = self.observe()
        names = self.names()
        with self.lock:
            roles = {}
            for side in ROLES:
                intent = self.intent[side]
                native = observed.get(side, {}).get("on")
                kelvin = intent["kelvin"]
                # HomeKit shows the verified lamp, not an unconfirmed request.
                shown = native if self.unconfirmed[side] and native is not None else intent["on"]
                roles[side] = {
                    "on": shown,
                    "requested_on": intent["on"],
                    "confirmed": self.confirmed[side],
                    "native_on": native,
                    "reachable": native is not None,
                    "brightness": intent["brightness"],
                    "hue": intent["hue"],
                    "saturation": intent["saturation"],
                    "color_temperature": round(1e6 / kelvin) if kelvin else None,
                }
                if side in names:
                    # Only an owner name is reported; without one HomeKit keeps its own.
                    roles[side]["name"] = names[side]
            return {"version": 1, "roles": roles}

    def set(self, payload):
        if (type(payload) is not dict or payload.get("role") not in ROLES
                or not set(payload) - {"role"} or set(payload) - _FIELDS - {"role"}
                or ("on" in payload and type(payload["on"]) is not bool)
                or ("brightness" in payload and (type(payload["brightness"]) is not int
                                                 or not 0 <= payload["brightness"] <= 100))
                or ("hue" in payload and not _number(payload["hue"], 0, 360))
                or ("saturation" in payload and not _number(payload["saturation"], 0, 100))
                or ("color_temperature" in payload
                    and (type(payload["color_temperature"]) is not int
                         or not 140 <= payload["color_temperature"] <= 500))):
            raise ValueError("Invalid HomeKit intent")
        side = payload["role"]
        with self.transitions[side]:
            if payload.get("brightness") == 0:
                # Like the Govee plugin: 0% is a coordinated Off, not a brightness.
                payload = {**payload, "on": False}
                del payload["brightness"]
            if "on" in payload:
                # An owner power change during Sync ends the Sync hold: it wins.
                self.owner_power(side)
            with self.lock:
                intent = self.intent[side]
                previous = dict(intent)
                if "brightness" in payload:
                    intent["brightness"] = payload["brightness"]
                if "hue" in payload or "saturation" in payload:
                    intent["hue"] = payload.get("hue", intent["hue"] or 0)
                    intent["saturation"] = payload.get("saturation", intent["saturation"] or 0)
                    intent["kelvin"] = None
                if "color_temperature" in payload:
                    intent["kelvin"] = max(2000, min(9000,
                                                     round(1e6 / payload["color_temperature"])))
                    intent["hue"] = intent["saturation"] = None
                self._save()
            confirmed = True
            if "on" in payload:
                confirmed = self._power(side, payload["on"])
            if confirmed and set(payload) & {"brightness", "hue", "saturation",
                                             "color_temperature"}:
                # The last explicit setting wins, as in the Hue App: a Home
                # brightness or color ends the Hue scene on this lamp. Power
                # alone keeps the scene, so Home On resumes it.
                self.release(side)
            if confirmed and self.allowed(side) and self.idle(side):
                failed = self._apply_static(side, payload)
                if failed:
                    # HomeKit must keep showing what the lamp actually has:
                    # restore only the values whose command was not delivered.
                    keys = {"brightness": ("brightness",),
                            "color": ("hue", "saturation", "kelvin")}
                    with self.lock:
                        for name in failed:
                            for key in keys[name]:
                                self.intent[side][key] = previous[key]
                        self._save()
                    confirmed = False
            return confirmed

    def _apply_static(self, side, payload):
        """Home color applies only while no scene or Sync owns the lamp.

        An explicit Home brightness or color has already ended a Hue scene
        (``release``); Sync output still keeps the lamp.

        Returns the names ("brightness", "color") of commands that were not
        delivered to a verified route.
        """
        device, intent = self.devices[side], self.intent[side]
        commands = []
        if ("brightness" in payload or payload.get("on") is True) and intent["brightness"]:
            commands.append(("brightness", "brightness", {"value": intent["brightness"]}))
        if set(payload) & {"hue", "saturation", "color_temperature"} or payload.get("on") is True:
            if intent["kelvin"] is not None:
                commands.append(("color", "colorwc", {"color": {"r": 255, "g": 255, "b": 255},
                                                      "colorTemInKelvin": intent["kelvin"]}))
            elif intent["hue"] is not None:
                red, green, blue = (round(channel * 255) for channel in colorsys.hsv_to_rgb(
                    intent["hue"] / 360, intent["saturation"] / 100, 1))
                commands.append(("color", "colorwc", {"color": {"r": red, "g": green, "b": blue},
                                                      "colorTemInKelvin": 0}))
        failed = set()
        with self.lock:
            for name, command, data in commands:
                try:
                    if self.sender(device, command, data) is not True:
                        failed.add(name)
                except OSError:
                    failed.add(name)
        return failed

    def _power(self, side, on):
        device = self.devices[side]
        with self.lock:
            # Holding the gate lock here waits out any in-flight color send.
            self.intent[side]["on"] = on
            self.confirmed[side] = False
            self.unconfirmed[side] = True
            self._changed(side, "intent")
            self.disagree_since[side] = None
            self._save()
            started = self.last_command[side] = self.clock()
            self.sender(device, "turn", {"value": 1 if on else 0})
        resent = False
        while self.clock() - started < CONFIRM_SECONDS:
            observed = self.observe().get(side, {})
            sample = observed.get("sample")
            if observed.get("on") is on and type(sample) in (int, float) and sample > started:
                with self.lock:
                    self.unconfirmed[side] = False
                    if on and self.intent[side]["on"] is True:
                        self.confirmed[side] = True
                        self._changed(side, "confirmed")
                return True
            if not resent and self.clock() - started >= RESEND_SECONDS:
                resent = True
                with self.lock:
                    self.last_command[side] = self.clock()
                    self.sender(device, "turn", {"value": 1 if on else 0})
            self.sleep(0.05)
        return False

    # Sync hold ------------------------------------------------------------

    def _send_turn(self, side, on):
        with self.lock:
            sent = self.last_command[side] = self.clock()
            self.sender(self.devices[side], "turn", {"value": 1 if on else 0})
            return sent

    def sync_hold(self, side, send=True):
        """Switch a lamp off for Sync without changing intent; True when it is held.

        A lamp that is already off is not held. Without ``send`` (crash recovery)
        the hold is placed without a command, so tick() cannot adopt the Off.
        """
        if side not in ROLES:
            raise ValueError("Unknown Sync hold role")
        with self.transitions[side]:
            native = self.observe().get(side, {}).get("on") if send else None
            with self.lock:
                if side in self.holds:
                    return True
                if send and (native if native is not None
                             else self.intent[side]["on"]) is not True:
                    return False
                self.holds.add(side)
                self._changed(side, "sync_hold")
                self.disagree_since[side] = None
            if send:
                switch_power(lambda on: self._send_turn(side, on), self.observe, side, False,
                             self.clock, self.sleep)
            return True

    def sync_held(self, side):
        with self.lock:
            return side in self.holds

    def sync_release(self, side):
        """Restore On after Sync unless the owner changed the lamp.

        Returns "restored", "superseded" (no hold left, or the owner wants Off) or
        "failed"; a failed restore keeps the hold, so the session can retry it.
        """
        with self.transitions[side]:
            with self.lock:
                if side not in self.holds:
                    return "superseded"
                self.holds.discard(side)
                self._changed(side, "sync_release")
                if self.intent[side]["on"] is False:
                    return "superseded"
            restored = False
            try:
                restored = self._power(side, True)
            finally:
                if not restored:
                    with self.lock:
                        self.holds.add(side)
                        self._changed(side, "sync_restore_failed")
            return "restored" if restored else "failed"

    def owner_power(self, side):
        """An explicit owner power request for this lamp ends its Sync hold."""
        with self.lock:
            if side in self.holds:
                self.holds.discard(side)
                self._changed(side, "owner_power")

    # Native observation ---------------------------------------------------

    def tick(self):
        """Adopt verified native changes made outside HomeKit, such as the Govee app."""
        observed = self.observe()
        now = self.clock()
        for side in ROLES:
            if not self.transitions[side].acquire(blocking=False):
                continue
            try:
                native = observed.get(side, {}).get("on")
                with self.lock:
                    intent = self.intent[side]["on"]
                    if side in self.holds:
                        # A Sync hold's Off is never adopted as owner intent.
                        self.disagree_since[side] = None
                    elif native is None:
                        self.disagree_since[side] = None
                    elif intent is None:
                        self._adopt(side, native)
                    elif native is intent and self.unconfirmed[side]:
                        # A late native reading confirms the earlier command.
                        self.unconfirmed[side] = False
                        if intent and not self.confirmed[side]:
                            self.confirmed[side] = True
                            self._changed(side, "confirmed")
                    elif intent and native and not self.confirmed[side]:
                        self.confirmed[side] = True
                        self._changed(side, "confirmed")
                    elif native is not intent and now - self.last_command[side] >= SETTLE_SECONDS:
                        if self.disagree_since[side] is None:
                            self.disagree_since[side] = now
                        elif now - self.disagree_since[side] >= ADOPT_SECONDS:
                            self._adopt(side, native)
                    else:
                        self.disagree_since[side] = None
            finally:
                self.transitions[side].release()

    def _adopt(self, side, native):
        self.intent[side]["on"] = native
        self.confirmed[side] = native
        self.unconfirmed[side] = False
        self._changed(side, "adopted")
        self.disagree_since[side] = None
        try:
            self._save()
        except OSError:
            pass


_ID = re.compile(r"[0-9a-f]{32}")
SIGNATURE_WINDOW = 2.0
NONCE_SECONDS = 10.0


def signature(token, method, path, timestamp, nonce, body):
    """HMAC-SHA256 request signature; the credential itself never travels."""
    message = "\n".join((method, path, timestamp, nonce, hashlib.sha256(body).hexdigest()))
    return hmac.new(token.encode(), message.encode(), hashlib.sha256).hexdigest()


def response_signature(token, nonce, status, body):
    """Binds a response to the signed request's nonce; distinct from requests."""
    message = "\n".join(("response", nonce, str(status), hashlib.sha256(body).hexdigest()))
    return hmac.new(token.encode(), message.encode(), hashlib.sha256).hexdigest()


class SignatureVerifier:
    """Accept each signed request once, within a short clock window."""

    def __init__(self, token, wall_clock=time.time):
        self.token, self.wall_clock = token, wall_clock
        self.lock = threading.Lock()
        self.seen = {}

    def verify(self, method, path, headers, body):
        timestamp = headers.get("X-LCC-Timestamp", "")
        nonce = headers.get("X-LCC-Nonce", "")
        provided = headers.get("X-LCC-Signature", "")
        try:
            issued = float(timestamp)
        except ValueError:
            return False
        if (not re.fullmatch(r"\d{1,12}(\.\d{1,6})?", timestamp) or not _ID.fullmatch(nonce)
                or not re.fullmatch(r"[0-9a-f]{64}", provided)):
            return False
        now = self.wall_clock()
        expected = signature(self.token, method, path, timestamp, nonce, body)
        if not hmac.compare_digest(provided, expected) or abs(now - issued) > SIGNATURE_WINDOW:
            return False
        with self.lock:
            self.seen = {key: seen for key, seen in self.seen.items()
                         if now - seen <= NONCE_SECONDS}
            if nonce in self.seen or len(self.seen) >= 4096:
                return False
            self.seen[nonce] = now
        return True


class HomeKitRequests:
    """Hue App power requests executed by the local authority."""

    def __init__(self, power, wall_clock=time.time):
        self.power, self.wall_clock = power, wall_clock

    def request(self, payload):
        if (type(payload) is not dict or set(payload) != {"id", "issued_at", "states"}
                or not isinstance(payload["id"], str) or not _ID.fullmatch(payload["id"])
                or not _number(payload["issued_at"], -math.inf, math.inf)
                or abs(self.wall_clock() - payload["issued_at"]) > MAX_MESSAGE_AGE
                or type(payload["states"]) is not dict or not payload["states"]
                or set(payload["states"]) - set(ROLES)
                or any(type(value) is not bool for value in payload["states"].values())):
            raise ValueError("Invalid explicit power request")
        for side, on in payload["states"].items():
            # The Hue app repeats On with every scene; a settled lamp needs no write.
            if self.power.settled(side, on):
                continue
            if not self.power.set({"role": side, "on": on}):
                raise PowerRequestUnavailable("Power request was not confirmed")
        return True

    def exchange(self, envelope, capability, observed, accepted):
        return {}

    def close(self):
        pass
