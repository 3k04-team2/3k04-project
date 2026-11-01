"""1.3.2 Dispensing Manager.

Secret: the exact workflow and timing of the cabinet: login, showing an order
before it is dispensed, unlocking, the 10 s relock, the 60 s limits, the
pharmacist deposit checks, and what is sent to the HMS when a transaction ends.
It receives every input event and door-sensor event, asks the State Machine
for the next mode and the Alarm Policy to sound alarms.

Implements FR-DSP-01/02, FR-SEC-02/03/04/08/09/10, FR-IVT-01..05,
FR-HMS-01/03/04, NFR-DSP-01, NFR-SEC-01/02/04.

Waiting for the HMS to check a password (up to 500 ms) happens outside the
lock, so the timers and the alarm keep running meanwhile.
"""

import threading

from adc.behaviour import state_machine as sm
from adc.behaviour.alarm_policy import NotAllowed
from adc.software.auth import WARN_AFTER

RELOCK_AFTER = 10.0
DOOR_OPEN_LIMIT = 60.0
RETRIEVE_LIMIT = 60.0
CAPACITY_WARNING = 0.95
WITHDRAW_ROLES = ("Doctor", "Nurse")
DEPOSIT_ROLES = ("Pharmacist",)
UNLOCKED_STATES = (sm.DISPENSE, sm.DEPOSIT, sm.DOOR_OPEN)
SERVICE_INPUTS = ("dismiss", "reset", "disarm", "logout", "maintenance")


def _ok(message="", **extra):
    return dict(extra, ok=True, message=message)


def _fail(message, **extra):
    return dict(extra, ok=False, message=message)


class DispensingManager:
    def __init__(self, adc_id, timer, display, door, keyboard, alarm_hw, alarms, machine,
                 auth, inventory, logger, comm):
        self._id = adc_id
        self._timer = timer
        self._display = display
        self._door = door
        self._alarm_hw = alarm_hw
        self._alarms = alarms
        self._machine = machine
        self._auth = auth
        self._inventory = inventory
        self._logger = logger
        self._comm = comm
        self._lock = threading.RLock()
        self._tx = None                 # the open transaction (unlocked door)
        self._selected = None           # order shown on screen, not yet dispensed (FR-DSP-02)
        self._pending_deposit = None    # order scanned, waiting for the quantity check (FR-IVT-04)
        self._capacity_flagged = inventory.level() >= CAPACITY_WARNING
        keyboard.registerInputHandler(self.handleInput)
        door.registerDoorSensorHandler(self._on_door)
        comm.registerNewOrderHandler(self.handleNewOrder)
        machine.registerStateListener(self._on_state)
        door.lockDoor()
        if machine.getState() == sm.LOCKED_OUT:
            display.showBlockingMessage("LOCKED_OUT")
        else:
            display.showMessage("IDLE")

    # ---- inputs -------------------------------------------------------------------
    def handleInput(self, event):
        handler = getattr(self, "_in_" + event.kind, None)
        if handler is None:
            return _fail("unknown input")
        self._auth.refreshSession()
        if self._display.isBlocked() and event.kind not in SERVICE_INPUTS:
            return _fail("Dismiss the message on the screen first.")
        return handler(event.data)

    def _in_login(self, d):
        user, password = str(d.get("user", "")), str(d.get("password", ""))
        with self._lock:
            st = self._machine.getState()
            if st == sm.LOCKED_OUT:
                return _fail(self._display.showBlockingMessage("LOCKED_OUT"))
            if st != sm.IDLE:
                return _fail("Someone is already using the cabinet.")
            if self._machine.isMaintenance():
                return _fail(self._display.showMessage("MAINTENANCE"))
        res = self._auth.authenticate(user, password)
        with self._lock:
            if self._machine.getState() != sm.IDLE:
                return _fail("The cabinet changed state; try again.")
            if res.ok:
                self._machine.handleEvent("LOGIN_OK")
                return _ok(self._display.showMessage("WELCOME", user=user, role=res.role),
                           access="granted", role=res.role)
            if self._auth.isLockedOut():
                self._lockout("5 failed login attempts")
                return _fail(self._display.read()["blocking"], access="denied", lockedOut=True)
            if res.reason == "unreachable":
                return _fail(self._display.showMessage("HMS_UNREACHABLE", 10), access="denied")
            if res.reason == "role":
                return _fail(self._display.showMessage("ROLE_NOT_ALLOWED", 10, role=res.role), access="denied")
            if res.failures >= WARN_AFTER:
                return _fail(self._display.showMessage("LOGIN_WARNING", count=res.failures),
                             access="denied", warning=True)
            return _fail(self._display.showMessage("ACCESS_DENIED", 5), access="denied")

    def _in_logout(self, d):
        with self._lock:
            st = self._machine.getState()
            if st in (sm.DISPENSE, sm.DEPOSIT):
                self.cancelDispense()
                st = self._machine.getState()
            if st != sm.AUTHORIZED:
                return _fail("Nobody is logged in.")
            self._machine.handleEvent("LOGOUT")
            return _ok(self._display.showMessage("SESSION_ENDED", 5))

    def _in_select_order(self, d):
        oid = str(d.get("order", "")).strip()
        with self._lock:
            problem = self._need(sm.AUTHORIZED, WITHDRAW_ROLES)
            if problem:
                return problem
            self._auth.refreshSession()
            if not self._inventory.isOrderPresented(oid):
                # FR-DSP-01: dispensing of that order is refused; the user stays logged in to retry
                self._selected = None
                self._logger.logEvent("INVALID_ORDER", user=self._auth.getCurrentUser(), details={"order": oid})
                return _fail(self._display.showMessage("INVALID_ORDER", order=oid or "(blank)"))
            o = self._inventory.getOrder(oid)
            self._selected = oid
            return _ok(self._display.showMessage("ORDER_DETAILS", order=oid, medication=o["medication"],
                                                 quantity=o["quantity"], compartment=o["compartment"]),
                       order=o)

    def _in_dispense(self, d):
        with self._lock:
            problem = self._need(sm.AUTHORIZED, WITHDRAW_ROLES)
            if problem:
                return problem
            if self._selected is None:
                return _fail("Select an order first; it is shown before it is dispensed.")
            if not self._door.isDoorClosed():
                return _fail("The door must be closed first.")
            return self.startDispense(self._selected)

    def _in_scan_deposit(self, d):
        oid = str(d.get("order", "")).strip()
        destination = str(d.get("destination", "") or self._id).strip()
        with self._lock:
            problem = self._need(sm.AUTHORIZED, DEPOSIT_ROLES)
            if problem:
                return problem
            self._auth.refreshSession()
            self._pending_deposit = None
            o = self._inventory.getOrder(oid)
            if destination != self._id:
                return self._block_deposit(oid, "wrong ADC", "WRONG_ADC", order=oid,
                                           destination=destination, adc=self._id)
            if o is None or o["status"] not in ("Pending", "Stocked"):
                return self._block_deposit(oid, "not on order list", "NOT_ASSIGNED", order=oid or "(blank)")
            if o["status"] == "Stocked":
                return self._block_deposit(oid, "already stocked", "ALREADY_STOCKED", order=oid,
                                           compartment=o["compartment"])
            if self._inventory.isFull():
                return self._block_deposit(oid, "cabinet full", "CABINET_FULL",
                                           capacity=self._inventory.capacity())
            k = o["compartment"] if self._inventory.isCompartmentFree(o["compartment"]) \
                else self._inventory.getFreeCompartment()
            self._pending_deposit = dict(o, compartment=k)
            return _ok(self._display.showMessage("CONFIRM_QUANTITY", order=oid, medication=o["medication"],
                                                 quantity=o["quantity"]),
                       prompt="confirm_quantity", order=self._pending_deposit)

    def _in_confirm_quantity(self, d):
        matches = d.get("matches") is True
        with self._lock:
            problem = self._need(sm.AUTHORIZED, DEPOSIT_ROLES)
            if problem:
                return problem
            p = self._pending_deposit
            if p is None:
                return _fail("Scan an order first.")
            self._pending_deposit = None
            if not matches:
                return self._block_deposit(p["order"], "quantity not confirmed", "QUANTITY_NOT_CONFIRMED",
                                           order=p["order"])
            if not self._door.isDoorClosed():
                return _fail("The door must be closed first.")
            self._logger.logEvent("QUANTITY_CONFIRMED", user=self._auth.getCurrentUser(),
                                  details={"order": p["order"], "quantity": p["quantity"]})
            self._open_tx("deposit", p)
            self._machine.handleEvent("DEPOSIT_CONFIRMED")
            return _ok(self._display.showMessage("DOOR_UNLOCKED"))

    def _in_compartment_done(self, d):
        with self._lock:
            tx = self._tx
            if self._machine.getState() != sm.DOOR_OPEN or tx is None or tx["done"]:
                return _fail("Nothing to take out or put in right now.")
            if tx["mode"] == "withdraw":
                self.confirmRetrieval()
            else:
                self._finish_deposit()
            return _ok(self._display.showMessage("CLOSE_DOOR"))

    def _in_cancel(self, d):
        with self._lock:
            if self._machine.getState() in (sm.DISPENSE, sm.DEPOSIT):
                self.cancelDispense()
                return _ok(self._display.showMessage("CANCELLED", 5))
            if self._selected or self._pending_deposit:
                self._selected = self._pending_deposit = None
                return _ok(self._display.showMessage("CANCELLED", 5))
            return _fail("Nothing to cancel.")

    def _in_dismiss(self, d):
        with self._lock:
            if self._machine.getState() in (sm.LOCKED_OUT, sm.INVALID):
                return _fail("An administrator or technician must reset the cabinet.")
            self._display.clearBlockingMessage()
            return _ok()

    def _in_reset(self, d):
        """FR-SEC-04: only an administrator or technician leaves Locked Out (or Invalid)."""
        with self._lock:
            st = self._machine.getState()
            if st not in (sm.LOCKED_OUT, sm.INVALID):
                return _fail("The cabinet is not locked out.")
            if not self._door.isDoorClosed():
                return _fail("Close the door before resetting.")
            if self._door.isTampered():
                return _fail("The tamper switch is still active; repair the cabinet first.")
        user = str(d.get("user", ""))
        role = self._auth.verifyService(user, str(d.get("password", "")))
        with self._lock:
            if role is None:
                return _fail("Access denied: only an administrator or technician can reset.")
            if self._machine.getState() not in (sm.LOCKED_OUT, sm.INVALID):
                return _fail("The cabinet is not locked out.")
            self._auth.clearLockedOut(user)
            self._machine.handleEvent("RESET")
            self._display.clearBlockingMessage()
            self._logger.logEvent("RESET", user=user, details={"role": role})
            self._logger.flushLog(self._comm)
            msg = self._display.showMessage("RESET_DONE", 10, user=user)
            if self._alarms.getActiveAlarm():
                msg += " The alarm is still sounding; disarm it."
            return _ok(msg)

    def _in_disarm(self, d):
        """FR-SEC-07."""
        if not self._alarms.getActiveAlarm():
            return _fail("No alarm is sounding.")
        user = str(d.get("user", ""))
        role = self._auth.verifyService(user, str(d.get("password", "")))
        with self._lock:
            try:
                cleared = self._alarms.acknowledgeAlarm(user, role, str(d.get("notes", "")))
            except NotAllowed as e:
                return _fail("Access denied: %s." % e)
            return _ok(self._display.showMessage("ALARM_DISARMED", 10, user=user), cleared=cleared)

    def _in_maintenance(self, d):
        enable = d.get("enable") is True
        with self._lock:
            if self._machine.getState() != sm.IDLE:
                return _fail("Maintenance mode can only change while the cabinet is idle.")
        user = str(d.get("user", ""))
        role = self._auth.verifyService(user, str(d.get("password", "")))
        with self._lock:
            if role is None:
                return _fail("Access denied: only an administrator or technician.")
            self._machine.setMaintenanceMode(enable)
            if enable:
                self._alarm_hw.alarmSuppression()
            else:
                self._alarm_hw.alarmRestore()
            self._logger.logEvent("MAINTENANCE_ON" if enable else "MAINTENANCE_OFF", user=user)
            return _ok(self._display.showMessage("MAINTENANCE") if enable else self._display.showMessage("IDLE"))

    # ---- exported operations (PDF interface) -------------------------------------------
    def startDispense(self, orderID):
        with self._lock:
            o = self._inventory.getOrder(orderID)
            self._open_tx("withdraw", o)
            self._tx["request"] = self._timer.startTiming()
            self._selected = None
            self._logger.logEvent("DISPENSE_REQUESTED", user=self._auth.getCurrentUser(),
                                  details={"order": orderID, "medication": o["medication"],
                                           "quantity": o["quantity"], "compartment": o["compartment"]})
            self._machine.handleEvent("ORDER_SELECTED")
            return _ok(self._display.showMessage("DOOR_UNLOCKED"), event="dispense_started", order=o)

    def cancelDispense(self):
        with self._lock:
            if self._machine.getState() not in (sm.DISPENSE, sm.DEPOSIT):
                return False
            self._logger.logEvent("TRANSACTION_CANCELLED", user=self._auth.getCurrentUser(),
                                  details={"order": self._tx["order"] if self._tx else ""})
            self._close_tx()
            self._machine.handleEvent("CANCEL")
            return True

    def confirmRetrieval(self):
        """i_dispenseComplete: the medication left the compartment."""
        with self._lock:
            tx = self._tx
            self._inventory.recordDispense(tx["order"])
            tx["done"] = True
            self._record_movement("withdraw", tx)
            self._comm.sendMessage("ORDER_STATUS", {"orders": [{"order": tx["order"], "status": "Fulfilled"}]})
            self._send_inventory()
            return {"event": "retrieved", "order": tx["order"]}

    def handleTimeout(self):
        """Called every tick: session timeout, 10 s relock, 60 s limits (handleTimeout(timer) in the PDF)."""
        with self._lock:
            st = self._machine.getState()
            tx = self._tx
            if st == sm.AUTHORIZED:
                active, _ = self._auth.checkSessionTime()
                if not active:
                    self._logger.logEvent("SESSION_TIMEOUT", user=self._auth.getCurrentUser())
                    self._machine.handleEvent("INACTIVE")
                    self._display.showMessage("SESSION_TIMEOUT", 10)
            elif st in (sm.DISPENSE, sm.DEPOSIT) and tx is not None:
                if self._door.isDoorClosed() and (self._timer.elapsed(tx["unlock"]) or 0) >= RELOCK_AFTER:
                    self._logger.logEvent("RELOCKED", user=self._auth.getCurrentUser(), details={"order": tx["order"]})
                    self._close_tx()
                    self._machine.handleEvent("RELOCK_TIMEOUT")
                    self._display.showMessage("RELOCKED", 10)
            elif st == sm.DOOR_OPEN and tx is not None:
                if (self._timer.elapsed(tx["door"]) or 0) > DOOR_OPEN_LIMIT:
                    self._alarms.reportCondition({"condition": "DOOR_OPEN_TOO_LONG"})
                    self._logger.logEvent("INVALID_STATE", details={"reason": "door open more than 60 s"})
                    self._machine.handleEvent("DOOR_OPEN_TIMEOUT")
                    self._display.showBlockingMessage("INVALID_STATE", reason="door open more than 60 s")
            tx = self._tx
            if (tx is not None and tx["mode"] == "withdraw" and not tx["done"] and not tx.get("late")
                    and (self._timer.elapsed(tx.get("request")) or 0) > RETRIEVE_LIMIT):
                tx["late"] = True
                self._alarms.reportCondition({"condition": "NOT_RETRIEVED"})

    tick = handleTimeout

    def getDispenseStatus(self):
        with self._lock:
            tx = self._tx
            out = {"state": self._machine.getState(), "selected": self._selected,
                   "pending_deposit": self._pending_deposit, "transaction": None}
            if tx is not None:
                out["transaction"] = {
                    "mode": tx["mode"], "order": tx["order"], "medication": tx["medication"],
                    "quantity": tx["quantity"], "compartment": tx["compartment"], "done": tx["done"],
                    "relock_in": max(0.0, RELOCK_AFTER - (self._timer.elapsed(tx["unlock"]) or 0))
                    if self._machine.getState() in (sm.DISPENSE, sm.DEPOSIT) else None,
                    "door_open_for": self._timer.elapsed(tx["door"]) if tx.get("door") else None}
            return out

    def handleNewOrder(self, orders):
        """ORDER_LIST from the HMS (FR-HMS-02); called on the HMS link's thread."""
        with self._lock:
            added, cancelled = self._inventory.reconcile(orders)
            self._logger.logEvent("ORDER_LIST_SYNCED", details={"orders": len(orders), "added": added,
                                                                "cancelled": cancelled})
            for oid in cancelled:
                self.handleCancelOrder(oid)

    def handleCancelOrder(self, orderID):
        with self._lock:
            if self._pending_deposit and self._pending_deposit["order"] == orderID:
                self._pending_deposit = None
                self._display.showMessage("CANCELLED", 5)
            if (self._tx and self._tx["order"] == orderID and self._tx["mode"] == "deposit"
                    and self._machine.getState() == sm.DEPOSIT):
                self.cancelDispense()
                self._display.showMessage("CANCELLED", 5)

    # ---- door sensor (interrupts) ----------------------------------------------------------
    def _on_door(self, event):
        with self._lock:
            self._auth.refreshSession()
            st = self._machine.getState()
            if event == "tamper":
                self._security("CABINET_DAMAGE", "tamper switch triggered")
            elif event == "opened":
                if st in (sm.DISPENSE, sm.DEPOSIT) and not self._door.isLocked():
                    tx = self._tx
                    self._timer.cancelTiming(tx["unlock"])
                    tx["door"] = self._timer.startTiming()
                    self._auth.refreshSession()
                    self._machine.handleEvent("DOOR_OPENED")
                    self._display.showMessage("TAKE_MEDICATION" if tx["mode"] == "withdraw" else "PLACE_MEDICATION",
                                              medication=tx["medication"], quantity=tx["quantity"],
                                              compartment=tx["compartment"])
                else:
                    self._security("FORCED_ENTRY", "door opened while latched")   # NAT-01
            elif event == "closed" and st == sm.DOOR_OPEN:
                tx = self._tx
                self._timer.cancelTiming(tx.get("door"))
                tx["door"] = None
                if tx["done"]:
                    self._machine.handleEvent("DOOR_CLOSED_DONE")
                    self._logger.flushLog(self._comm)
                    key = "DISPENSE_COMPLETE" if tx["mode"] == "withdraw" else "DEPOSIT_COMPLETE"
                    self._machine.handleEvent("LOG_SENT")
                    self._display.showMessage(key, 10, order=tx["order"], compartment=tx["compartment"])
                else:
                    tx["unlock"] = self._timer.startTiming()
                    self._machine.handleEvent("RETURN_TO_DISPENSE" if tx["mode"] == "withdraw" else "RETURN_TO_DEPOSIT")
                    self._display.showMessage("DOOR_UNLOCKED")

    def _on_state(self, old, new):
        """Applies the cabinet-lock REQ table and ends the session when the cabinet locks."""
        if new in UNLOCKED_STATES:
            self._door.unlockDoor()
        else:
            self._door.lockDoor()
        if new == sm.AUTHORIZED:
            self._close_tx()
            self._auth.refreshSession()
        if new in (sm.IDLE, sm.LOCKED_OUT, sm.INVALID):
            self._close_tx()
            self._selected = self._pending_deposit = None
            self._auth.endSession()
        self._logger.logEvent("STATE", details={"from": old, "to": new})

    # ---- internals ------------------------------------------------------------------------------
    def _need(self, state, roles):
        if self._machine.getState() != state:
            return _fail("Not possible in mode %s." % self._machine.getState())
        if self._auth.getCurrentRole() not in roles:
            return _fail("A %s cannot do this." % self._auth.getCurrentRole())
        return None

    def _block_deposit(self, oid, reason, messageID, **params):
        self._logger.logEvent("DEPOSIT_BLOCKED", user=self._auth.getCurrentUser(),
                              details={"order": oid, "reason": reason})
        return _fail(self._display.showBlockingMessage(messageID, **params), blocked=reason)

    def _open_tx(self, mode, o):
        self._tx = {"mode": mode, "order": o["order"], "medication": o["medication"], "quantity": o["quantity"],
                    "compartment": o["compartment"], "user": self._auth.getCurrentUser(), "done": False,
                    "unlock": self._timer.startTiming(), "door": None, "request": None}

    def _close_tx(self):
        tx, self._tx = self._tx, None
        if tx:
            for k in ("unlock", "door", "request"):
                if tx.get(k):
                    self._timer.cancelTiming(tx[k])

    def _finish_deposit(self):
        tx = self._tx
        self._inventory.recordDeposit(tx["order"], tx["compartment"])
        tx["done"] = True
        self._record_movement("deposit", tx)
        self._comm.sendMessage("ORDER_STATUS", {"orders": [{"order": tx["order"], "status": "Stocked"}]})
        self._send_inventory()

    def _record_movement(self, kind, tx):
        """FR-HMS-01: time, user, order number, medicine and quantity."""
        details = {"order": tx["order"], "medication": tx["medication"], "quantity": tx["quantity"],
                   "compartment": tx["compartment"]}
        self._logger.logEvent(kind.upper(), user=tx["user"], details=details)
        self._comm.sendMessage("EVENT", dict(details, kind=kind, user=tx["user"]))

    def _send_inventory(self):
        """FR-HMS-04: the inventory status carries a warning at 95 % capacity."""
        level = self._inventory.level()
        warn = level >= CAPACITY_WARNING
        if warn and not self._capacity_flagged:
            self._logger.logEvent("CAPACITY_WARNING", details={"level": round(level, 2)})
        self._capacity_flagged = warn
        self._comm.sendMessage("INVENTORY", {"compartments": self._inventory.report(),
                                             "occupancy": round(level, 3), "capacity_warning": warn})

    def _lockout(self, reason):
        self._auth.lockOut(reason)
        self._machine.handleEvent("FAILED_LOGIN_LIMIT")
        self._display.showBlockingMessage("LOCKED_OUT")
        self._logger.flushLog(self._comm)

    def _security(self, condition, text):
        """FR-SEC-06/08: forced entry or damage -> alarm on, Locked Out, cabinet locked."""
        self._alarms.reportCondition({"condition": condition, "text": text})
        self._auth.lockOut(text)
        self._machine.handleEvent(condition)
        self._display.showBlockingMessage("LOCKED_OUT")
        self._logger.flushLog(self._comm)
