"""1.3.1 State Machine Manager.

Secret: the operating modes (c_ADCState) and the transition table from the
"REQ for operational mode transitions" table of Deliverable 2. Nothing else
decides the next mode.

Additions to the PDF table, needed for a complete machine:
- Deposit mirrors Dispense for a pharmacist stocking an order, because the
  Dispense invariant only allows Doctor/Nurse (FR-DSP-01).
- Authorized -> Idle on logout; Dispense/Deposit -> Authorized on cancel.
- Door Open -> Dispense/Deposit if the door closes before the medication was
  taken out or placed in (the transaction is not finished).
- Invalid -> Idle on an administrator/technician reset.

One change: where the PDF table returns to Idle after a transaction (relock,
log sent), this machine returns to Authorized. c_sessionState is a separate
variable that only ends on inactivity (FR-SEC-09) or logout, so the user stays
logged in; the door still locks because Authorized is a locked mode.
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

# events that apply in any mode
FROM_ANY = {"FORCED_ENTRY": LOCKED_OUT, "CABINET_DAMAGE": LOCKED_OUT}


class TransitionResult:
    def __init__(self, ok, old, new, event):
        self.ok, self.old, self.new, self.event = ok, old, new, event

    def __repr__(self):
        return "TransitionResult(%s: %s -> %s%s)" % (self.event, self.old, self.new, "" if self.ok else ", refused")


class StateMachine:
    def __init__(self, initial=IDLE):
        if initial not in STATES:
            raise ValueError(initial)
        self._state = initial
        self._maintenance = False
        self._listeners = []
        self._lock = threading.RLock()

    def getState(self):
        return self._state

    def handleEvent(self, event):
        with self._lock:
            old = self._state
            new = FROM_ANY.get(event) or TRANSITIONS.get((old, event))
            if new is None or (event == "LOGIN_OK" and self._maintenance):
                return TransitionResult(False, old, old, event)
            self._state = new
        if new != old:
            for cb in list(self._listeners):
                cb(old, new)
        return TransitionResult(True, old, new, event)

    def setMaintenanceMode(self, enable):
        """While on, clinical users cannot log in (technician testing)."""
        self._maintenance = bool(enable)

    def isMaintenance(self):
        return self._maintenance

    def registerStateListener(self, callback):
        """callback(old, new) after every change of mode."""
        self._listeners.append(callback)
