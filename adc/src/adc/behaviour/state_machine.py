"""1.3.1 State Machine Manager.

Secret: the operating modes (c_ADCState), the transition table from the
"REQ for operational mode transitions" table of Deliverable 2 and the
conditions of the timed transitions. Nothing else decides the next mode.

Implements:
- FR-SEC-02: the cabinet stays locked in every mode except Dispense, Deposit
  and Door Open ("REQ for cabinet lock"); a failed login changes nothing.
- FR-SEC-05: Door Open for more than 60 s -> Invalid, and the alarm sounds.
- FR-SEC-08: the door sensor's "forced" or "tamper" event -> Locked Out.
- FR-SEC-09: Authorized with no activity for 5 minutes -> Idle (session ended).
- FR-SEC-10: Dispense/Deposit with the door still closed 10 s after unlocking
  -> the door locks again.
Timed transitions are checked on handleEvent("TICK"), sent every 50 ms.

Additions to the PDF table, needed for a complete machine:
- Deposit mirrors Dispense for a pharmacist stocking an order, because the
  Dispense invariant only allows Doctor/Nurse (FR-DSP-01).
- Authorized -> Idle on logout; Dispense/Deposit -> Authorized on cancel.
- Door Open -> Dispense/Deposit if the door closes before the medication was
  taken out or placed in (the transaction is not finished).
- Invalid -> Idle on an administrator/technician reset.
- Where the PDF table returns to Idle after a transaction (relock, log sent),
  this machine returns to Authorized: c_sessionState only ends on inactivity
  (FR-SEC-09) or logout, so the user stays logged in; the door still locks.
"""

import threading

IDLE = "Idle"
AUTHORIZED = "Authorized"
DISPENSE = "Dispense"
DEPOSIT = "Deposit"
DOOR_OPEN = "Door Open"
DOOR_CLOSED = "Door Closed"
INVALID = "Invalid"
LOCKED_OUT = "Locked Out"
STATES = (IDLE, AUTHORIZED, DISPENSE, DEPOSIT, DOOR_OPEN, DOOR_CLOSED, INVALID, LOCKED_OUT)
UNLOCKED_STATES = (DISPENSE, DEPOSIT, DOOR_OPEN)
NO_SESSION_STATES = (IDLE, LOCKED_OUT, INVALID)

RELOCK_AFTER = 10.0
DOOR_OPEN_LIMIT = 60.0

TRANSITIONS = {
    (IDLE, "LOGIN_OK"): AUTHORIZED,
    (IDLE, "FAILED_LOGIN_LIMIT"): LOCKED_OUT,
    (AUTHORIZED, "INACTIVE"): IDLE,
    (AUTHORIZED, "LOGOUT"): IDLE,
    (AUTHORIZED, "ORDER_SELECTED"): DISPENSE,
    (AUTHORIZED, "DEPOSIT_CONFIRMED"): DEPOSIT,
    (DISPENSE, "RELOCK_TIMEOUT"): AUTHORIZED,
    (DISPENSE, "DOOR_OPENED"): DOOR_OPEN,
    (DISPENSE, "CANCEL"): AUTHORIZED,
    (DEPOSIT, "RELOCK_TIMEOUT"): AUTHORIZED,
    (DEPOSIT, "DOOR_OPENED"): DOOR_OPEN,
    (DEPOSIT, "CANCEL"): AUTHORIZED,
    (DOOR_OPEN, "DOOR_CLOSED_DONE"): DOOR_CLOSED,
    (DOOR_OPEN, "RETURN_TO_DISPENSE"): DISPENSE,
    (DOOR_OPEN, "RETURN_TO_DEPOSIT"): DEPOSIT,
    (DOOR_OPEN, "DOOR_OPEN_TIMEOUT"): INVALID,
    (DOOR_CLOSED, "LOG_SENT"): AUTHORIZED,
    (LOCKED_OUT, "RESET"): IDLE,
    (INVALID, "RESET"): IDLE,
}

# events that apply in any mode (FR-SEC-08)
FROM_ANY = {"FORCED_ENTRY": LOCKED_OUT, "CABINET_DAMAGE": LOCKED_OUT}
DOOR_EVENTS = {"forced": ("FORCED_ENTRY", "cabinet forced open"),
               "tamper": ("CABINET_DAMAGE", "tamper switch triggered")}


class TransitionResult:
    def __init__(self, ok, old, new, event):
        self.ok, self.old, self.new, self.event = ok, old, new, event

    def __repr__(self):
        return "TransitionResult(%s: %s -> %s%s)" % (self.event, self.old, self.new, "" if self.ok else ", refused")


class StateMachine:
    def __init__(self, timer, door, auth, alarms, display, logger):
        self._timer = timer
        self._door = door
        self._auth = auth
        self._alarms = alarms
        self._display = display
        self._logger = logger
        self._state = LOCKED_OUT if auth.isLockedOut() else IDLE
        self._entered = timer.startTiming()
        self._maintenance = False
        self._listeners = []
        self._lock = threading.RLock()
        door.registerDoorSensorHandler(self._on_door)
        door.lockDoor()
        if self._state == LOCKED_OUT:
            display.showBlockingMessage("LOCKED_OUT")
        else:
            display.showMessage("IDLE")

    # ---- exported operations ------------------------------------------------------
    def getState(self):
        return self._state

    def handleEvent(self, event):
        """event: a name from TRANSITIONS / FROM_ANY, or "TICK" to check the timed transitions."""
        if event == "TICK":
            event = self._timed_event()
            if event is None:
                return TransitionResult(False, self._state, self._state, "TICK")
        with self._lock:
            old = self._state
            new = FROM_ANY.get(event) or TRANSITIONS.get((old, event))
            if new is None or (event == "LOGIN_OK" and self._maintenance):
                return TransitionResult(False, old, old, event)
            self._state = new
            self._timer.cancelTiming(self._entered)
            self._entered = self._timer.startTiming()
        self._enter(old, new, event)
        if new != old:
            for cb in list(self._listeners):
                cb(old, new, event)
        return TransitionResult(True, old, new, event)

    def setMaintenanceMode(self, enable):
        """While on, clinical users cannot log in (technician testing)."""
        self._maintenance = bool(enable)

    def isMaintenance(self):
        return self._maintenance

    def registerStateListener(self, callback):
        """callback(old, new, event) after every change of mode."""
        self._listeners.append(callback)

    def getTimers(self):
        """Seconds left before the relock, and how long the door has been open (for the panel)."""
        st, t = self._state, self._timer.elapsed(self._entered) or 0.0
        return {"relock_in": max(0.0, RELOCK_AFTER - t) if st in (DISPENSE, DEPOSIT) else None,
                "door_open_for": t if st == DOOR_OPEN else None}

    # ---- internals ------------------------------------------------------------------
    def _timed_event(self):
        st, t = self._state, self._timer.elapsed(self._entered) or 0.0
        if st == AUTHORIZED and not self._auth.checkSessionTime()[0]:
            return "INACTIVE"                                                    # FR-SEC-09
        if st in (DISPENSE, DEPOSIT) and self._door.isDoorClosed() and t >= RELOCK_AFTER:
            return "RELOCK_TIMEOUT"                                              # FR-SEC-10
        if st == DOOR_OPEN and t > DOOR_OPEN_LIMIT:
            return "DOOR_OPEN_TIMEOUT"                                           # FR-SEC-05
        return None

    def _on_door(self, event):
        if event in DOOR_EVENTS:
            name, reason = DOOR_EVENTS[event]
            self._auth.lockOut(reason)
            self.handleEvent(name)

    def _enter(self, old, new, event):
        """What entering a mode means: the cabinet lock, the session and the screen."""
        if new in UNLOCKED_STATES:
            self._door.unlockDoor()
        else:
            self._door.lockDoor()
        user = self._auth.getCurrentUser()
        if event == "INACTIVE":
            self._logger.logEvent("SESSION_TIMEOUT", user=user)
            self._display.showMessage("SESSION_TIMEOUT", 10)
        elif event == "RELOCK_TIMEOUT":
            self._logger.logEvent("RELOCKED", user=user)
            self._display.showMessage("RELOCKED", 10)
        elif event == "DOOR_OPEN_TIMEOUT":
            self._alarms.reportCondition({"condition": "DOOR_OPEN_TOO_LONG"})
            self._logger.logEvent("INVALID_STATE", details={"reason": "door open more than 60 s"})
            self._display.showBlockingMessage("INVALID_STATE", reason="door open more than 60 s")
        if new == AUTHORIZED:
            self._auth.refreshSession()
        elif new in NO_SESSION_STATES:
            self._auth.endSession()
        if new == LOCKED_OUT:
            self._display.showBlockingMessage("LOCKED_OUT")
            self._logger.flushLog()
        elif old in (LOCKED_OUT, INVALID):
            self._display.clearBlockingMessage()
        if new != old:
            self._logger.logEvent("STATE", details={"from": old, "to": new})
