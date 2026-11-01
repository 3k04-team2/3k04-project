"""M8 Access Control.

Secret: how credentials are stored and checked (salted hashes), the session
timeout, the lockout rule and the role-permission table. Implements SPEC-10.
Uses: M3 ADC Gateway, M9 Log Store, M10 State Store, M12 Host Platform.
"""

import hashlib
import hmac
import os
import secrets
import threading

from hms.errors import NotAllowed, SessionExpired
from hms.protocol import Message

SESSION_TIMEOUT = 600.0
LOCKOUT_AFTER = 5
LOCKOUT_SECONDS = 3600.0
PBKDF2_ROUNDS = 100_000

DEFAULT_PERMS = {
    "Doctor": ["view", "submit_order", "ack_alarm"],
    "Nurse": ["view", "ack_alarm"],
    "Pharmacist": ["view", "mark_fulfilled", "ack_alarm"],
    "Technician": ["view", "ack_alarm", "clear_maintenance"],
    "Administrator": ["view", "view_logs"],
    "IT": ["view", "view_logs", "manage_roles"],
}

# Demo accounts, created only when the State Store has no users yet.
DEMO_USERS = {
    "doctor1": "Doctor", "nurse1": "Nurse", "pharmacist1": "Pharmacist",
    "tech1": "Technician", "admin1": "Administrator", "it1": "IT",
}


def demo_password(user):
    return user + "!demo"


class BadCredentials(Exception):
    pass


class DeviceLocked(Exception):
    pass


class Session:
    def __init__(self, user, role, device, now):
        self.id = secrets.token_hex(16)
        self.user = user
        self.role = role
        self.device = device
        self.last = now


def _hash(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)


class AccessControl:
    def __init__(self, gateway, log, state, host):
        self._gateway = gateway
        self._log = log
        self._state = state
        self._host = host
        self._lock = threading.RLock()
        self._sessions = {}
        self._fails = {}
        self._locked_until = {}
        self._users = state.load("users")
        self._perms = state.load("perms") or {r: list(p) for r, p in DEFAULT_PERMS.items()}
        if not self._users:
            self._users = {}
            for name, role in DEMO_USERS.items():
                salt = os.urandom(16)
                self._users[name] = {"salt": salt.hex(), "hash": _hash(demo_password(name), salt).hex(),
                                     "role": role}
            state.save("users", self._users)
            state.save("perms", self._perms)

    # ---- sessions -------------------------------------------------------
    def login(self, user, password, device):
        now = self._host.monotonic()
        with self._lock:
            until = self._locked_until.get(device)
            if until is not None:
                if now < until:
                    self._log.append({"user": user, "type": "LOGIN_BLOCKED", "data": "device " + device})
                    raise DeviceLocked(device)
                del self._locked_until[device]
                self._fails[device] = 0
            rec = self._users.get(user)
            salt = bytes.fromhex(rec["salt"]) if rec else b"\0" * 16
            good = hmac.compare_digest(_hash(password, salt).hex(), rec["hash"] if rec else "x")
            if not rec or not good:
                self._fails[device] = self._fails.get(device, 0) + 1
                self._log.append({"user": user, "type": "LOGIN_FAILED",
                                  "data": "device %s, failure %d" % (device, self._fails[device])})
                if self._fails[device] >= LOCKOUT_AFTER:
                    self._locked_until[device] = now + LOCKOUT_SECONDS
                    self._log.append({"user": user, "type": "DEVICE_LOCKED", "data": device})
                raise BadCredentials(user)
            self._fails[device] = 0
            session = Session(user, rec["role"], device, now)
            self._sessions[session.id] = session
            self._log.append({"user": user, "type": "LOGIN", "data": "role " + rec["role"]})
            return session

    def verify(self, user, password, adcId):
        """Check a login typed at an ADC; returns the role, or None.

        Starts no session and applies no device lockout: the ADC counts its own
        failures and locks itself out, and an administrator must still be able
        to log in at a locked-out ADC to reset it.
        """
        rec = self._users.get(user) if isinstance(user, str) else None
        salt = bytes.fromhex(rec["salt"]) if rec else b"\0" * 16
        good = isinstance(password, str) and hmac.compare_digest(
            _hash(password, salt).hex(), rec["hash"] if rec else "x")
        if rec and good:
            self._log.append({"adc": adcId, "user": user, "type": "ADC_LOGIN", "data": "role " + rec["role"]})
            return rec["role"]
        self._log.append({"adc": adcId, "user": str(user), "type": "ADC_LOGIN_FAILED", "data": ""})
        return None

    def logout(self, session):
        with self._lock:
            self._sessions.pop(session.id, None)
        self._log.append({"user": session.user, "type": "LOGOUT", "data": ""})

    def touch(self, session):
        with self._lock:
            live = self._sessions.get(session.id)
            if live:
                live.last = self._host.monotonic()

    def check(self, session):
        """Raise SessionExpired if the session has timed out. Does not count as activity."""
        now = self._host.monotonic()
        with self._lock:
            live = self._sessions.get(session.id)
            if live is None or now - live.last >= SESSION_TIMEOUT:
                self._sessions.pop(session.id, None)
                raise SessionExpired(session.user)

    def allowed(self, session, action):
        """True if the session's role may do the action. Exception: SessionExpired."""
        now = self._host.monotonic()
        with self._lock:
            live = self._sessions.get(session.id)
            if live is None or now - live.last >= SESSION_TIMEOUT:
                self._sessions.pop(session.id, None)
                raise SessionExpired(session.user)
            live.last = now
            return action in self._perms.get(live.role, [])

    def role_perms(self, role):
        return list(self._perms.get(role, []))

    # ---- role management (IT only) --------------------------------------
    def users(self, session):
        self._require(session, "manage_roles")
        return {u: r["role"] for u, r in self._users.items()}

    def setRole(self, session, user, role):
        self._require(session, "manage_roles")
        if user not in self._users or role not in self._perms:
            raise KeyError("unknown user or role")
        with self._lock:
            self._users[user]["role"] = role
            self._state.save("users", self._users)
            for s in self._sessions.values():
                if s.user == user:
                    s.role = role
        self._log.append({"user": session.user, "type": "SET_ROLE", "data": "%s -> %s" % (user, role)})
        return self._send_roles()

    def editRole(self, session, role, permissions):
        self._require(session, "manage_roles")
        with self._lock:
            self._perms[role] = sorted(set(permissions))
            self._state.save("perms", self._perms)
        self._log.append({"user": session.user, "type": "EDIT_ROLE",
                          "data": "%s: %s" % (role, ",".join(sorted(set(permissions))))})
        return self._send_roles()

    def roles_message(self):
        users = [{"user": u, "role": r["role"]} for u, r in self._users.items()]
        return Message("ROLE_UPDATE", body={"users": users})

    def _send_roles(self):
        return self._gateway.broadcast(self.roles_message())

    def _require(self, session, action):
        if not self.allowed(session, action):
            self._log.append({"user": session.user, "type": "DENIED", "data": action})
            raise NotAllowed(action)
