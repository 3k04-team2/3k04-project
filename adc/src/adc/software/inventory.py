"""1.2.2 Inventory Manager.

Secret: how orders and their compartments are recorded (c_orderInventoryRecord,
c_orderLocationRecord) and kept in non-volatile storage so they survive a power
loss (FR-IVT-01, FR-IVT-05, FR-HMS-06).

Order status here: Pending (on the HMS list, not stocked yet), Stocked,
Fulfilled, Cancelled.
"""

import threading

KEEP_FINISHED = 200


class InventoryManager:
    def __init__(self, nvram, capacity):
        self._nvram = nvram
        self._capacity = capacity
        self._lock = threading.RLock()
        self._orders = nvram.read("inventory", {}).get("orders", {})

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

    # ---- physical deposits and withdrawals ----------------------------------------
    def recordDeposit(self, orderID, compartmentID):
        with self._lock:
            cur = self._orders[orderID]
            cur["compartment"] = compartmentID
            cur["status"] = "Stocked"
            self._save()

    def recordDispense(self, orderID):
        with self._lock:
            self._orders[orderID]["status"] = "Fulfilled"
            self._save()

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

    def isCompartmentFree(self, compartmentID):
        with self._lock:
            return (isinstance(compartmentID, int) and 0 <= compartmentID < self._capacity
                    and all(not (o["status"] == "Stocked" and o["compartment"] == compartmentID)
                            for o in self._orders.values()))

    def getFreeCompartment(self):
        for k in range(self._capacity):
            if self.isCompartmentFree(k):
                return k
        return None

    def listInventory(self):
        with self._lock:
            return sorted(((oid, o["compartment"]) for oid, o in self._orders.items()
                           if o["status"] == "Stocked"), key=lambda x: x[1])

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

    def report(self):
        """Compartment list for the INVENTORY message."""
        return [{"compartment": o["compartment"], "medication": o["medication"], "quantity": o["quantity"]}
                for o in self.stocked()]

    def _save(self):
        finished = [oid for oid, o in self._orders.items() if o["status"] in ("Fulfilled", "Cancelled")]
        for oid in finished[:-KEEP_FINISHED] if len(finished) > KEEP_FINISHED else []:
            del self._orders[oid]
        self._nvram.write("inventory", {"orders": self._orders})
