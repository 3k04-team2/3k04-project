"""1.2.1 Authentication & Access Control.

Secret: the session record, the inactivity timeout, the failed-attempt counter
and the lockout rule. Passwords are checked by the HMS (through the HMS
communication module); this module never stores them. The failure count and
the lockout flag are kept in memory so cutting the power does not
undo a lockout.

Implements FR-SEC-01/02/03 (who may log in, counting failures, the lockout on
the 5th), FR-SEC-04 and FR-SEC-07 (only an administrator or technician may
reset a lockout or disarm the alarm), the session record behind FR-SEC-09,
NFR-SEC-01 (warning on the 3rd failure) and NFR-SEC-05 (the HMS link answers
within 500 ms or the login is refused).
"""

import threading

CLINICAL_ROLES = ("Doctor", "Nurse", "Pharmacist")
SERVICE_ROLES = ("Administrator", "Technician")
SESSION_TIMEOUT = 300.0
WARN_AFTER = 3
LOCKOUT_AFTER = 5


class AuthResult:
    def __init__(self, ok, role="", reason="", failures=0):
        self.ok = ok
        self.role = role
        self.reason = reason          # "", "denied", "unreachable", "role", "locked"
        self.failures = failures

    @property
    def warning(self):
        return not self.ok and self.reason == "denied" and self.failures >= WARN_AFTER


class AuthManager:
    def __init__(self, comm, timer, memory, logger):
        self._comm = comm
        self._timer = timer
        self._memory = memory
        self._logger = logger
        self._lock = threading.RLock()
        saved = memory.read("auth", {})
        self._failures = saved.get("failures", 0)
        self._locked = saved.get("locked", False)
        self._user = None
        self._role = None
        self._activity = None

    # ---- logging in --------------------------------------------------------------
    def authenticate(self, userId, password):
        """Ask the HMS; count failures; start a session for clinical roles."""
        if self._locked:
            return AuthResult(False, reason="locked", failures=self._failures)
        status, role = self._comm.checkAuth(userId, password)
        with self._lock:
            if status == "unreachable":
                self._logger.logEvent("LOGIN_UNVERIFIED", user=userId, details={"reason": "HMS unreachable"})
                return AuthResult(False, reason="unreachable", failures=self._failures)
            if status != "ok":
                self._failures += 1
                if self._failures >= LOCKOUT_AFTER:
                    self._locked = True
                self._save()
                self._logger.logEvent("LOGIN_FAILED", user=userId, details={"failures": self._failures})
                return AuthResult(False, reason="denied", failures=self._failures)
            self._failures = 0
            self._save()
            if role not in CLINICAL_ROLES:
                self._logger.logEvent("LOGIN_REFUSED_ROLE", user=userId, details={"role": role})
                return AuthResult(False, role=role, reason="role")
            self._user, self._role = userId, role
            self._activity = self._timer.startTiming()
            self._logger.logEvent("LOGIN", user=userId, details={"role": role})
            return AuthResult(True, role=role)

    def verifyService(self, userId, password):
        """Check an administrator/technician for reset, disarm or maintenance (FR-SEC-04/07).

        Does not start a session and does not count toward the lockout, so a
        locked-out cabinet can still be reset. reason: "unreachable", "denied" or "role".
        """
        status, role = self._comm.checkAuth(userId, password)
        ok = status == "ok" and role in SERVICE_ROLES
        self._logger.logEvent("SERVICE_AUTH" if ok else "SERVICE_AUTH_FAILED", user=userId,
                              details={"role": role or "", "status": status})
        if ok:
            return AuthResult(True, role=role)
        return AuthResult(False, role=role or "", reason="role" if status == "ok" else status)

    # ---- the session ----------------------------------------------------------------
    def endSession(self):
        with self._lock:
            if self._user is not None:
                self._logger.logEvent("LOGOUT", user=self._user)
            if self._activity is not None:
                self._timer.cancelTiming(self._activity)
            self._user = self._role = self._activity = None

    def isSessionActive(self):
        return self._user is not None

    def getCurrentUser(self):
        return self._user

    def getCurrentRole(self):
        return self._role

    def refreshSession(self):
        with self._lock:
            if self._user is not None:
                self._timer.cancelTiming(self._activity)
                self._activity = self._timer.startTiming()

    def checkSessionTime(self):
        """(still active, seconds left before the inactivity timeout)."""
        with self._lock:
            if self._user is None:
                return False, 0.0
            left = SESSION_TIMEOUT - (self._timer.elapsed(self._activity) or 0.0)
            return left > 0, max(0.0, left)

    # ---- lockout ----------------------------------------------------------------------
    def failures(self):
        return self._failures

    def isLockedOut(self):
        return self._locked

    def lockOut(self, reason):
        with self._lock:
            if not self._locked:
                self._locked = True
                self._save()
                self._logger.logEvent("LOCKED_OUT", details={"reason": reason})

    def clearLockedOut(self, userId):
        with self._lock:
            self._locked = False
            self._failures = 0
            self._save()
            self._logger.logEvent("LOCKOUT_CLEARED", user=userId)

    def _save(self):
        self._memory.write("auth", {"failures": self._failures, "locked": self._locked})
