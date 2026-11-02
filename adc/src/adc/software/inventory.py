"""1.2.2 Inventory Manager.

Secret: how orders and their compartments are recorded (c_orderInventoryRecord,
c_orderLocationRecord) and kept in non-volatile storage so they survive a power
loss, and the rules for what may be deposited.

Implements FR-IVT-01 and FR-IVT-05 (every order and its compartment),
FR-IVT-02/03/04 (deposit blocked for another ADC's order, a full cabinet or an
unconfirmed quantity), FR-HMS-02 (reconcile with the HMS order list), FR-HMS-04
(inventory status with the 95 % warning) and FR-HMS-06.

Order status here: Pending (on the HMS list, not stocked yet), Stocked,
Fulfilled, Cancelled.
"""

import threading

KEEP_FINISHED = 200
CAPACITY_WARNING = 0.95


class InventoryManager:
    def __init__(self, adc_id, nvram, capacity, comm):
        self._id = adc_id
        self._nvram = nvram
        self._capacity = capacity
        self._comm = comm
        self._lock = threading.RLock()
        self._orders = nvram.read("inventory", {}).get("orders", {})
        self._confirmed = set()         # orders whose quantity the pharmacist confirmed (FR-IVT-04)

    # ---- orders from the HMS ------------------------------------------------
    def addPendingOrder(self, orderID, orderDetails, compartmentID):
        with self._lock:
            self._orders[orderID] = {"medication": orderDetails.get("medication", ""),
                                     "quantity": orderDetails.get("quantity", 0),
                                     "compartment": compartmentID, "status": "Pending"}
            self._save()

    def reconcile(self, snapshot):
        """Bring the local list in line with the HMS order list for this ADC.

        snapshot: [{order, compartment, medication, quantity}]. New orders become
        Pending; Pending orders the HMS no longer lists are Cancelled. Stocked
        orders stay, because the medication is physically inside.
        Returns (added ids, cancelled ids).
        """
        added, cancelled = [], []
        with self._lock:
            listed = set()
            for o in snapshot:
                oid = o.get("order")
                if not isinstance(oid, str):
                    continue
                listed.add(oid)
                cur = self._orders.get(oid)
                if cur is None or cur["status"] == "Cancelled":
                    self._orders[oid] = {"medication": o.get("medication", ""), "quantity": o.get("quantity", 0),
                                         "compartment": o.get("compartment"), "status": "Pending"}
                    added.append(oid)
                elif cur["status"] == "Pending":
                    cur.update(medication=o.get("medication", ""), quantity=o.get("quantity", 0),
                               compartment=o.get("compartment"))
            for oid, cur in self._orders.items():
                if cur["status"] == "Pending" and oid not in listed:
                    cur["status"] = "Cancelled"
                    cancelled.append(oid)
            self._save()
        return added, cancelled

    def cancelOrder(self, orderID):
        with self._lock:
            cur = self._orders.get(orderID)
            if cur is None or cur["status"] != "Pending":
                return False
            cur["status"] = "Cancelled"
            self._save()
            return True

    # ---- deposit rules (FR-IVT-02/03/04) -------------------------------------------
    def checkDeposit(self, orderID, destinationADC):
        """(None, compartment) if the scanned package may be stocked, else (reason, None)."""
        with self._lock:
            o = self._orders.get(orderID)
            if destinationADC != self._id:
                return "wrong ADC", None
            if o is None or o["status"] not in ("Pending", "Stocked"):
                return "not on order list", None
            if o["status"] == "Stocked":
                return "already stocked", None
            if self.isFull():
                return "cabinet full", None
            k = o["compartment"] if self._is_free(o["compartment"]) else self.getFreeCompartment()
            return None, k

    def confirmQuantity(self, orderID, matches):
        """The pharmacist's count; a deposit is only recorded after a match."""
        with self._lock:
            if matches:
                self._confirmed.add(orderID)
            else:
                self._confirmed.discard(orderID)
            return bool(matches)

    # ---- physical deposits and withdrawals ----------------------------------------
    def recordDeposit(self, orderID, compartmentID):
        with self._lock:
            if orderID not in self._confirmed:
                raise ValueError("quantity of %s not confirmed" % orderID)
            self._confirmed.discard(orderID)
            cur = self._orders[orderID]
            cur["compartment"] = compartmentID
            cur["status"] = "Stocked"
            self._save()
        self._report("deposit", orderID)

    def recordDispense(self, orderID):
        with self._lock:
            self._orders[orderID]["status"] = "Fulfilled"
            self._save()
        self._report("withdraw", orderID)

    # ---- queries -------------------------------------------------------------------
    def getOrder(self, orderID):
        with self._lock:
            cur = self._orders.get(orderID)
            return dict(cur, order=orderID) if cur else None

    def getOrderStatus(self, orderID):
        cur = self._orders.get(orderID)
        return cur["status"] if cur else None

    def getCompartment(self, orderID):
        cur = self._orders.get(orderID)
        return cur["compartment"] if cur and cur["status"] == "Stocked" else None

    def isOrderPresented(self, orderID):
        """True if the order is physically inside the cabinet."""
        return self.getOrderStatus(orderID) == "Stocked"

    def getFreeCompartment(self):
        for k in range(self._capacity):
            if self._is_free(k):
                return k
        return None

    def listInventory(self):
        return [(o["order"], o["compartment"]) for o in self.stocked()]

    def stocked(self):
        with self._lock:
            return sorted((dict(o, order=oid) for oid, o in self._orders.items() if o["status"] == "Stocked"),
                          key=lambda o: o["compartment"])

    def pending(self):
        with self._lock:
            return [dict(o, order=oid) for oid, o in self._orders.items() if o["status"] == "Pending"]

    def capacity(self):
        return self._capacity

    def occupancy(self):
        with self._lock:
            return sum(1 for o in self._orders.values() if o["status"] == "Stocked")

    def level(self):
        return self.occupancy() / self._capacity if self._capacity else 1.0

    def isFull(self):
        return self.occupancy() >= self._capacity

    # ---- internals -------------------------------------------------------------------
    def _is_free(self, k):
        return (isinstance(k, int) and 0 <= k < self._capacity
                and all(not (o["status"] == "Stocked" and o["compartment"] == k) for o in self._orders.values()))

    def _report(self, kind, orderID):
        """The movement (EVENT) and the inventory status with the 95 % warning (FR-HMS-04)."""
        o = self.getOrder(orderID)
        self._comm.sendMessage("EVENT", {"kind": kind, "order": orderID, "medication": o["medication"],
                                         "quantity": o["quantity"], "compartment": o["compartment"]})
        level = self.level()
        self._comm.sendMessage("INVENTORY", {
            "compartments": [{"compartment": s["compartment"], "medication": s["medication"],
                              "quantity": s["quantity"]} for s in self.stocked()],
            "occupancy": round(level, 3), "capacity_warning": level >= CAPACITY_WARNING})

    def _save(self):
        finished = [oid for oid, o in self._orders.items() if o["status"] in ("Fulfilled", "Cancelled")]
        for oid in finished[:-KEEP_FINISHED] if len(finished) > KEEP_FINISHED else []:
            del self._orders[oid]
        self._nvram.write("inventory", {"orders": self._orders})
