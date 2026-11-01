"""M5 Order Manager.

Secret: the order lifecycle and how compartments are chosen. Implements the
order part of SPEC-04 and SPEC-05, and SPEC-06.
Uses: M3, M4, M6, M7, M8, M9, M10.
"""

import threading
import time

from hms.errors import NotAllowed
from hms.gateway import AdcUnavailable, NotAcknowledged, BoundsError
from hms.host import DiskError
from hms.inventory import AdcFull
from hms.protocol import Message

DEFAULT_FORMULARY = {"Morphine": 100, "Insulin": 100, "Amoxicillin": 500,
                     "Ibuprofen": 1000, "Heparin": 100}
ACTIVE = ("Pending", "Placed", "Stocked")

# the order status table of SPEC-05: (current status, event) -> next status
TRANSITIONS = {
    ("Pending", "ack"): "Placed",
    ("Pending", "timeout"): "Failed",
    ("Pending", "cancel"): "Cancelled",
    ("Placed", "cancel"): "Cancelled",
    ("Placed", "deposit"): "Stocked",
    ("Stocked", "withdraw"): "Fulfilled",
    ("Placed", "manual"): "Fulfilled",
    ("Stocked", "manual"): "Fulfilled",
}
EVENT_FOR_STATUS = {"Stocked": "deposit", "Fulfilled": "withdraw"}


class OrderError(Exception):
    def __init__(self, reason, suggestions=None):
        Exception.__init__(self, reason)
        self.reason = reason
        self.suggestions = suggestions or []


class InvalidInput(OrderError):
    pass


class AlarmBlocking(OrderError):
    pass


class AlreadyFulfilled(OrderError):
    pass


class UnknownOrder(OrderError):
    pass


class AdcFullError(OrderError):
    pass


class AdcOffline(OrderError):
    pass


class OrderManager:
    def __init__(self, gateway, health, inventory, alarms, access, log, state, clock=time.time):
        self._clock = clock
        self._gateway = gateway
        self._health = health
        self._inventory = inventory
        self._alarms = alarms
        self._access = access
        self._log = log
        self._state = state
        self._lock = threading.RLock()
        self._orders = {}
        self._n = 0
        for o in state.load("orders", []):
            self._orders[o["id"]] = o
            self._n = max(self._n, int(o["id"].split("-")[1]))
        self._formulary = state.load("formulary") or dict(DEFAULT_FORMULARY)

    def formulary(self):
        return dict(self._formulary)

    # ---- SPEC-05 -------------------------------------------------------------
    def submit(self, session, adcId, med, qty, confirmed=False):
        if not self._access.allowed(session, "submit_order"):
            self._deny(session, "submit_order")
        problem = self._check(adcId, med, qty, confirmed)
        with self._lock:
            if problem is None:
                try:
                    k = self._inventory.reserve(adcId)
                except AdcFull:
                    problem = AdcFullError("ADC %s has no free compartment" % adcId)
            if problem is not None:
                problem.suggestions = self._suggest(adcId)
                self._log.append({"adc": adcId, "user": session.user, "type": "ORDER_REFUSED",
                                  "data": "%s x%s: %s" % (med, qty, problem.reason)})
                if isinstance(problem, AdcFullError):
                    # a full ADC refuses the order and raises an alarm (FR-OPR-02)
                    self._alarms.raise_("ADC_FULL", "warning", adcId,
                                        "ADC %s has no free compartment; order refused" % adcId)
                raise problem
            self._n += 1
            order = {"id": "ORD-%d" % self._n, "adc": adcId, "doctor": session.user, "medication": med,
                     "quantity": qty, "compartment": k, "status": "Pending",
                     "time": self._clock()}
            self._orders[order["id"]] = order
            self._save()
        self._log.append({"adc": adcId, "user": session.user, "type": "ORDER_SUBMITTED",
                          "data": "%s: %s x%s in compartment %d" % (order["id"], med, qty, k)})
        try:
            self._send_list(adcId)
        except (AdcUnavailable, NotAcknowledged, BoundsError) as e:
            self._set(order, TRANSITIONS[("Pending", "timeout")])
            self._inventory.release(adcId, k)
            self._alarms.raise_("ORDER_FAILED", "warning", adcId, "%s was not acknowledged" % order["id"])
            self._alarms.notify(session.user, "Order %s failed: the ADC did not acknowledge" % order["id"])
            raise AdcOffline("order %s failed: %s" % (order["id"], e))
        self._set(order, TRANSITIONS[("Pending", "ack")])
        self._alarms.notify("Pharmacist", "Order %s: %s x%s for ADC %s, compartment %d" %
                            (order["id"], med, qty, adcId, k))
        return order["id"]

    def _check(self, adcId, med, qty, confirmed):
        if not confirmed:
            return InvalidInput("the order has not been confirmed")
        if not isinstance(qty, int) or isinstance(qty, bool) or not 1 <= qty <= 1000:
            return InvalidInput("quantity must be a whole number from 1 to 1000")
        if med not in self._formulary:
            return InvalidInput("%s is not in the formulary" % med)
        if qty > self._formulary[med]:
            return InvalidInput("quantity is above the maximum of %d for %s" % (self._formulary[med], med))
        if self._health.state(adcId) != "Online":
            return AdcOffline("ADC %s is %s" % (adcId, self._health.state(adcId)))
        if self._health.maintenance(adcId):
            return AdcOffline("ADC %s is flagged for maintenance" % adcId)
        if any(a["adc"] == adcId for a in self._alarms.active()):
            return AlarmBlocking("ADC %s has an unacknowledged alarm" % adcId)
        if self._inventory.free(adcId) < 1:
            return AdcFullError("ADC %s has no free compartment" % adcId)
        return None

    def _suggest(self, adcId):
        status = self._health.systemStatus()["adcs"]
        return [a for a, s in status.items()
                if a != adcId and s["state"] == "Online" and not s["maintenance"]
                and self._inventory.free(a) >= 1]

    # ---- cancel and manual fulfil -------------------------------------------------
    def cancel(self, session, orderId):
        if not self._access.allowed(session, "submit_order"):
            self._deny(session, "submit_order")
        order = self._get(orderId)
        nxt = TRANSITIONS.get((order["status"], "cancel"))
        if nxt is None:
            raise OrderError("only Pending or Placed orders can be cancelled")
        self._set(order, nxt)
        self._inventory.release(order["adc"], order["compartment"])
        self._log.append({"adc": order["adc"], "user": session.user, "type": "ORDER_CANCELLED", "data": orderId})
        try:
            self._send_list(order["adc"])
        except (AdcUnavailable, NotAcknowledged, BoundsError):
            self._alarms.raise_("ORDER_SYNC", "warning", order["adc"], "cancel of %s not confirmed" % orderId)

    def markFulfilled(self, session, orderId):
        """SPEC-06."""
        if not self._access.allowed(session, "mark_fulfilled"):
            self._deny(session, "mark_fulfilled")
        order = self._get(orderId)
        if order["status"] == "Fulfilled":
            raise AlreadyFulfilled("order %s is already fulfilled" % orderId)
        if (order["status"], "manual") not in TRANSITIONS:
            raise OrderError("order %s is %s" % (orderId, order["status"]))
        if self._health.state(order["adc"]) != "Online":
            raise AdcOffline("ADC %s is not Online" % order["adc"])
        before = order["status"]
        self._set(order, TRANSITIONS[(before, "manual")])
        try:
            self._send_list(order["adc"])
        except (AdcUnavailable, NotAcknowledged, BoundsError):
            self._set(order, before)                     # revert, then say so
            self._alarms.raise_("ORDER_SYNC", "critical", order["adc"], "fulfil of %s not confirmed" % orderId)
            raise AdcOffline("the ADC did not acknowledge; the change was reverted")
        self._inventory.release(order["adc"], order["compartment"])
        self._log.append({"adc": order["adc"], "user": session.user, "type": "ORDER_FULFILLED_BY_HAND",
                          "data": orderId})

    # ---- handlers registered with M3 and M4 ---------------------------------------------
    def onOrderStatus(self, adcId, msg):
        for item in msg.body.get("orders", []):
            order = self._orders.get(item.get("order"))
            if not order or order["adc"] != adcId:
                continue
            event = EVENT_FOR_STATUS.get(item.get("status"))
            nxt = TRANSITIONS.get((order["status"], event))
            if nxt is None:
                self._log.append({"adc": adcId, "type": "ORDER_STATUS_IGNORED",
                                  "data": "%s: %s -> %s not allowed" % (order["id"], order["status"], item.get("status"))})
                continue
            self._set(order, nxt)
            if nxt == "Fulfilled":
                self._inventory.release(adcId, order["compartment"])

    def onAdcStateChange(self, adcId, old, new):
        if new != "Offline":
            return
        with self._lock:
            at_risk = [o for o in self._orders.values()
                       if o["adc"] == adcId and o["status"] in ("Placed", "Stocked")]
        for o in at_risk:
            self._alarms.raise_("ORDER_AT_RISK", "critical", adcId,
                                "order %s (%s x%s) is stored in an Offline ADC" % (o["id"], o["medication"], o["quantity"]))
            self._alarms.notify(o["doctor"], "Order %s is at risk: ADC %s is Offline" % (o["id"], adcId))

    def resync(self, adcId):
        """After an ADC reconnects: send it the full order list again (run in its own thread)."""
        try:
            self._send_list(adcId)
        except (AdcUnavailable, NotAcknowledged, BoundsError):
            pass

    def orders(self, adcId=None):
        with self._lock:
            return [dict(o) for o in self._orders.values() if adcId in (None, o["adc"])][::-1]

    # ---- internals ----------------------------------------------------------------
    def _send_list(self, adcId):
        with self._lock:
            body = {"orders": [{"order": o["id"], "compartment": o["compartment"],
                                "medication": o["medication"], "quantity": o["quantity"]}
                               for o in self._orders.values()
                               if o["adc"] == adcId and o["status"] in ACTIVE]}
        self._gateway.send(adcId, Message("ORDER_LIST", body=body))

    def _get(self, orderId):
        order = self._orders.get(orderId)
        if order is None:
            raise UnknownOrder("no order " + orderId)
        return order

    def _set(self, order, status):
        with self._lock:
            order["status"] = status
            self._save()
        self._log.append({"adc": order["adc"], "type": "ORDER_STATUS", "data": "%s -> %s" % (order["id"], status)})

    def _deny(self, session, action):
        self._log.append({"user": session.user, "type": "DENIED", "data": action})
        raise NotAllowed(action)

    def _save(self):
        try:
            self._state.save("orders", list(self._orders.values()))
        except DiskError:
            pass
