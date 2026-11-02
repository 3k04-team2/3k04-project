"""1.3.2 Dispensing Manager.

Secret: the exact workflow of a transaction: logging in, showing an order
before it is dispensed, unlocking for a withdrawal or a deposit, the
pharmacist's scan and quantity prompt, what happens when the compartment is
used and the door closes, and the order status sent to the HMS.

Implements FR-DSP-01 (dispense the specified order; an order that is not
stocked is refused), FR-DSP-02 (the order ID and medication are shown before
dispensing) and FR-HMS-03 (ORDER_STATUS when an order is stocked or fulfilled).
It receives every keyboard input and the door's opened/closed events, and asks
Authentication (who may act), the Inventory Manager (what may be stocked), the
State Machine (the next mode), the Alarm Policy (disarm) and the Event Logger
(the FR-HMS-01 record of each withdrawal and deposit).

Waiting for the HMS to check a password (up to 500 ms) happens outside the
lock, so the timers and the alarm keep running meanwhile.
"""

import threading

from adc.behaviour import state_machine as sm

WITHDRAW_ROLES = ("Doctor", "Nurse")
DEPOSIT_ROLES = ("Pharmacist",)
SERVICE_INPUTS = ("dismiss", "reset", "disarm", "logout", "maintenance")
DEPOSIT_BLOCKS = {"wrong ADC": "WRONG_ADC", "not on order list": "NOT_ASSIGNED",
                  "already stocked": "ALREADY_STOCKED", "cabinet full": "CABINET_FULL"}


def _ok(message="", **extra):
    return dict(extra, ok=True, message=message)


def _fail(message, **extra):
    return dict(extra, ok=False, message=message)


class DispensingManager:
    def __init__(self, adc_id, display, door, keyboard, alarm_hw, alarms, machine, auth, inventory, logger, comm):
        self._id = adc_id
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
        keyboard.registerInputHandler(self.handleInput)
        door.registerDoorSensorHandler(self._on_door)
        comm.registerNewOrderHandler(self.handleNewOrder)
        machine.registerStateListener(self._on_state)

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
                self._machine.handleEvent("FAILED_LOGIN_LIMIT")
                return _fail(self._display.read()["blocking"], access="denied", lockedOut=True)
            if res.reason == "unreachable":
                return _fail(self._display.showMessage("HMS_UNREACHABLE", 10), access="denied")
            if res.reason == "role":
                return _fail(self._display.showMessage("ROLE_NOT_ALLOWED", 10, role=res.role), access="denied")
            if res.warning:
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
            if not self._inventory.isOrderPresented(oid):
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
            self._pending_deposit = None
            reason, k = self._inventory.checkDeposit(oid, destination)
            if reason:
                return self._block_deposit(oid, reason, DEPOSIT_BLOCKS[reason], order=oid or "(blank)",
                                           destination=destination, adc=self._id,
                                           compartment=self._inventory.getCompartment(oid),
                                           capacity=self._inventory.capacity())
            o = self._inventory.getOrder(oid)
            self._pending_deposit = dict(o, compartment=k)
            return _ok(self._display.showMessage("CONFIRM_QUANTITY", order=oid, medication=o["medication"],
                                                 quantity=o["quantity"]),
                       prompt="confirm_quantity", order=self._pending_deposit)

    def _in_confirm_quantity(self, d):
        with self._lock:
            problem = self._need(sm.AUTHORIZED, DEPOSIT_ROLES)
            if problem:
                return problem
            p, self._pending_deposit = self._pending_deposit, None
            if p is None:
                return _fail("Scan an order first.")
            if not self._inventory.confirmQuantity(p["order"], d.get("matches") is True):
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
        """FR-SEC-04: leaving Locked Out (or Invalid) needs an administrator or technician."""
        with self._lock:
            if self._machine.getState() not in (sm.LOCKED_OUT, sm.INVALID):
                return _fail("The cabinet is not locked out.")
            if not self._door.isDoorClosed():
                return _fail("Close the door before resetting.")
            if self._door.isTampered():
                return _fail("The tamper switch is still active; repair the cabinet first.")
        user = str(d.get("user", ""))
        res = self._auth.verifyService(user, str(d.get("password", "")))
        with self._lock:
            if not res.ok:
                return self._service_refused(res, "can reset")
            if self._machine.getState() not in (sm.LOCKED_OUT, sm.INVALID):
                return _fail("The cabinet is not locked out.")
            self._auth.clearLockedOut(user)
            self._machine.handleEvent("RESET")
            self._logger.logEvent("RESET", user=user, details={"role": res.role})
            self._logger.flushLog()
            msg = self._display.showMessage("RESET_DONE", 10, user=user)
            if self._alarms.getActiveAlarm():
                msg += " The alarm is still sounding; disarm it."
            return _ok(msg)

    def _in_disarm(self, d):
        """FR-SEC-07."""
        if not self._alarms.getActiveAlarm():
            return _fail("No alarm is sounding.")
        user = str(d.get("user", ""))
        res = self._auth.verifyService(user, str(d.get("password", "")))
        with self._lock:
            if not res.ok:
                return self._service_refused(res, "can disarm the alarm")
            cleared = self._alarms.acknowledgeAlarm(user, str(d.get("notes", "")))
            return _ok(self._display.showMessage("ALARM_DISARMED", 10, user=user), cleared=cleared)

    def _in_maintenance(self, d):
        enable = d.get("enable") is True
        with self._lock:
            if self._machine.getState() != sm.IDLE:
                return _fail("Maintenance mode can only change while the cabinet is idle.")
        user = str(d.get("user", ""))
        res = self._auth.verifyService(user, str(d.get("password", "")))
        with self._lock:
            if not res.ok:
                return self._service_refused(res, "can change maintenance mode")
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
            self._tx = None
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
            return {"event": "retrieved", "order": tx["order"]}

    def explainLockedDoor(self):
        """Someone pulled the handle while the latch was engaged: say what to do first."""
        with self._lock:
            st = self._machine.getState()
            if st in (sm.LOCKED_OUT, sm.INVALID):
                return _fail(self._display.showMessage("DOOR_LOCKED_RESET"))
            if st == sm.IDLE:
                return _fail(self._display.showMessage("DOOR_LOCKED_LOGIN"))
            self._auth.refreshSession()
            return _fail(self._display.showMessage("DOOR_LOCKED_SELECT"))

    def handleTimeout(self, timer=None):
        """Called every 50 ms: the State Machine checks its timed transitions."""
        with self._lock:
            self._machine.handleEvent("TICK")

    def getDispenseStatus(self):
        with self._lock:
            tx = self._tx
            return dict(self._machine.getTimers(), state=self._machine.getState(), selected=self._selected,
                        pending_deposit=self._pending_deposit,
                        transaction={k: tx[k] for k in ("mode", "order", "medication", "quantity",
                                                        "compartment", "done")} if tx else None)

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
        """Opening and closing during a transaction; tamper goes to the Alarm Policy
        and the State Machine."""
        with self._lock:
            self._auth.refreshSession()
            st, tx = self._machine.getState(), self._tx
            if event == "opened" and st in (sm.DISPENSE, sm.DEPOSIT):
                self._machine.handleEvent("DOOR_OPENED")
                self._display.showMessage("TAKE_MEDICATION" if tx["mode"] == "withdraw" else "PLACE_MEDICATION",
                                          medication=tx["medication"], quantity=tx["quantity"],
                                          compartment=tx["compartment"])
            elif event == "closed" and st == sm.DOOR_OPEN:
                if tx["done"]:
                    self._machine.handleEvent("DOOR_CLOSED_DONE")
                    self._logger.flushLog()
                    key = "DISPENSE_COMPLETE" if tx["mode"] == "withdraw" else "DEPOSIT_COMPLETE"
                    self._machine.handleEvent("LOG_SENT")
                    self._display.showMessage(key, 10, order=tx["order"], compartment=tx["compartment"])
                else:
                    self._machine.handleEvent("RETURN_TO_DISPENSE" if tx["mode"] == "withdraw" else "RETURN_TO_DEPOSIT")
                    self._display.showMessage("DOOR_UNLOCKED")

    def _on_state(self, old, new, event):
        """A finished, cancelled or interrupted transaction is closed."""
        with self._lock:
            if new == sm.AUTHORIZED or new in sm.NO_SESSION_STATES:
                self._tx = None
            if new in sm.NO_SESSION_STATES:
                self._selected = self._pending_deposit = None

    # ---- internals ------------------------------------------------------------------------------
    def _need(self, state, roles):
        if self._machine.getState() != state:
            return _fail("Not possible in mode %s." % self._machine.getState())
        if self._auth.getCurrentRole() not in roles:
            return _fail("A %s cannot do this." % self._auth.getCurrentRole())
        return None

    def _service_refused(self, res, action):
        if res.reason == "unreachable":
            return _fail(self._display.showMessage("SERVICE_UNREACHABLE", 10))
        return _fail("Access denied: only an administrator or technician %s." % action)

    def _block_deposit(self, oid, reason, messageID, **params):
        self._logger.logEvent("DEPOSIT_BLOCKED", user=self._auth.getCurrentUser(),
                              details={"order": oid, "reason": reason})
        return _fail(self._display.showBlockingMessage(messageID, **params), blocked=reason)

    def _open_tx(self, mode, o):
        self._tx = {"mode": mode, "order": o["order"], "medication": o["medication"], "quantity": o["quantity"],
                    "compartment": o["compartment"], "user": self._auth.getCurrentUser(), "done": False}

    def _finish_deposit(self):
        tx = self._tx
        self._inventory.recordDeposit(tx["order"], tx["compartment"])
        tx["done"] = True
        self._record_movement("deposit", tx)
        self._comm.sendMessage("ORDER_STATUS", {"orders": [{"order": tx["order"], "status": "Stocked"}]})

    def _record_movement(self, kind, tx):
        """FR-HMS-01: time, user, order number, medicine and quantity (the logger adds the time)."""
        self._logger.logEvent(kind.upper(), user=tx["user"],
                              details={"order": tx["order"], "medication": tx["medication"],
                                       "quantity": tx["quantity"], "compartment": tx["compartment"]})
