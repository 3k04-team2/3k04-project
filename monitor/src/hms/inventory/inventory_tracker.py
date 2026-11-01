"""M6 Inventory Tracker.

Secret: how compartment contents and expected counts are stored and compared.
Implements SPEC-07 (count check) and SPEC-08 (capacity). Uses: M7, M9, M10.
(is_online, passed in by main.py, is M4's state operation; it only decides
whether data is shown as stale.)
"""

import threading

from hms.host import DiskError

OCCUPANCY_ALERT = 0.95


class AdcFull(Exception):
    pass


class InventoryTracker:
    def __init__(self, alarms, log, state, is_online=None):
        self._alarms = alarms
        self._log = log
        self._state = state
        self._is_online = is_online or (lambda adc: True)
        self._lock = threading.RLock()
        saved = state.load("inventory", {})
        self._expected = {tuple(k.split("|", 1)): v for k, v in saved.get("expected", {}).items()}
        self._reserved = {a: set(ks) for a, ks in saved.get("reserved", {}).items()}
        self._capacity = {a: c for a, c in saved.get("capacity", {}).items()}
        self._contents = {}
        self._updated = {}
        self._flagged = set()
        self._last_mismatch = {}

    def onHello(self, adcId, msg):
        with self._lock:
            self._capacity[adcId] = msg.body.get("capacity", 0)
            self._reserved.setdefault(adcId, set())
            self._save()

    # ---- handlers registered with M3 ------------------------------------------
    def onEvent(self, adcId, msg):
        b = msg.body
        med, qty = b.get("medication", ""), b.get("quantity", 0)
        with self._lock:
            key = (adcId, med)
            delta = qty if b.get("kind") == "deposit" else -qty
            self._expected[key] = self._expected.get(key, 0) + delta
            negative = self._expected[key] < 0
            self._save()
        self._log.append({"adc": adcId, "user": b.get("user", ""), "type": "EVENT",
                          "data": "%s %s x%s (order %s)" % (b.get("kind"), med, qty, b.get("order", ""))})
        if negative:
            self._mismatch(adcId, med, self._expected[key], None)

    def onInventory(self, adcId, msg):
        comps = msg.body.get("compartments", [])
        reported = {}
        for c in comps:
            if c.get("medication"):
                reported[c["medication"]] = reported.get(c["medication"], 0) + c.get("quantity", 0)
        with self._lock:
            self._contents[adcId] = comps
            self._updated[adcId] = msg.time
            meds = {m for (a, m) in self._expected if a == adcId} | set(reported)
            expected = {m: self._expected.get((adcId, m), 0) for m in meds}
        for med in sorted(meds):
            if reported.get(med, 0) != expected[med]:
                self._mismatch(adcId, med, expected[med], reported.get(med, 0))
            else:
                self._last_mismatch.pop((adcId, med), None)
        self._check_capacity(adcId)

    # ---- compartment bookkeeping for M5 ---------------------------------------
    def free(self, adcId):
        with self._lock:
            return max(0, self._capacity.get(adcId, 0) - len(self._reserved.get(adcId, ())))

    def reserve(self, adcId):
        with self._lock:
            used = self._reserved.setdefault(adcId, set())
            for k in range(self._capacity.get(adcId, 0)):
                if k not in used:
                    used.add(k)
                    self._save()
                    break
            else:
                k = None
        if k is None:
            self._alarms.raise_("ADC_FULL", "warning", adcId, "no free compartment")
            raise AdcFull(adcId)
        self._check_capacity(adcId)
        return k

    def release(self, adcId, k):
        with self._lock:
            self._reserved.get(adcId, set()).discard(k)
            self._save()
        self._check_capacity(adcId)

    def contents(self, adcId):
        with self._lock:
            return {"compartments": list(self._contents.get(adcId, [])),
                    "updated": self._updated.get(adcId),
                    "stale": not self._is_online(adcId),
                    "capacity": self._capacity.get(adcId, 0),
                    "free": max(0, self._capacity.get(adcId, 0) - len(self._reserved.get(adcId, ()))),
                    "occupancy": self._occupancy(adcId)}

    # ---- internals ----------------------------------------------------------------
    def _occupancy(self, adcId):
        cap = self._capacity.get(adcId, 0)
        return len(self._reserved.get(adcId, ())) / cap if cap else 0.0

    def _check_capacity(self, adcId):
        occ = self._occupancy(adcId)
        with self._lock:
            crossed = occ >= OCCUPANCY_ALERT and adcId not in self._flagged
            if crossed:
                self._flagged.add(adcId)
            elif occ < OCCUPANCY_ALERT:
                self._flagged.discard(adcId)
        if crossed:
            self._alarms.raise_("CAPACITY", "warning", adcId, "ADC is %.0f%% full" % (occ * 100))
            self._alarms.notify("Pharmacist", "ADC %s is %.0f%% full" % (adcId, occ * 100))

    def _mismatch(self, adcId, med, expected, reported):
        if self._last_mismatch.get((adcId, med)) == (expected, reported):
            return
        self._last_mismatch[(adcId, med)] = (expected, reported)
        text = "%s: expected %s, ADC reports %s" % (med, expected, reported)
        self._log.append({"adc": adcId, "type": "COUNT_MISMATCH", "data": text})
        self._alarms.raise_("COUNT_MISMATCH", "critical", adcId, text)

    def _save(self):
        try:
            self._state.save("inventory", {
                "expected": {"%s|%s" % k: v for k, v in self._expected.items()},
                "reserved": {a: sorted(ks) for a, ks in self._reserved.items()},
                "capacity": self._capacity})
        except DiskError:
            pass
