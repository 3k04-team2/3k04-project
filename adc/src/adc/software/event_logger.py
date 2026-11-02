"""1.2.3 Event Logger.

Secret: how activity is recorded and stored (a list in non-volatile storage)
and how it is packed for the HMS (LOG_BATCH entries).

Implements FR-HMS-01 (withdrawals and deposits with time, user, order,
medicine and quantity) and FR-HMS-05 (alarms with time, time to disarm, reason
and notes): every entry is time-stamped here, and flushLog sends the unsent
entries to the HMS.
"""

import json
import threading

MAX_KEPT = 5000
BATCH = 50
MAX_TEXT = 256


class EventLogger:
    def __init__(self, nvram, timer, comm):
        self._nvram = nvram
        self._timer = timer
        self._comm = comm
        self._lock = threading.RLock()
        self._events = nvram.read("activity_log", [])
        self._n = max([e["id"] for e in self._events], default=0)

    def logEvent(self, eventType, time=None, details=None, user=""):
        with self._lock:
            self._n += 1
            entry = {"id": self._n, "type": eventType,
                     "time": self._timer.timeNow() if time is None else time,
                     "user": user or "", "details": dict(details or {}), "sent": False}
            self._events.append(entry)
            if len(self._events) > MAX_KEPT:
                drop = len(self._events) - MAX_KEPT
                self._events = [e for i, e in enumerate(self._events) if i >= drop or not e["sent"]]
            self._save()
            return dict(entry)

    def getEvents(self, startTime=None, endTime=None, filter=None):
        """filter: an event type, a collection of types, or a function(entry) -> bool."""
        if filter is None:
            keep = lambda e: True
        elif callable(filter):
            keep = filter
        elif isinstance(filter, str):
            keep = lambda e: e["type"] == filter
        else:
            types = set(filter)
            keep = lambda e: e["type"] in types
        with self._lock:
            return [dict(e) for e in self._events
                    if (startTime is None or e["time"] >= startTime)
                    and (endTime is None or e["time"] <= endTime) and keep(e)]

    def flushLog(self):
        """Send every unsent entry to the HMS (sent now or queued by the HMS link). Returns the count."""
        with self._lock:
            unsent = [e for e in self._events if not e["sent"]]
            for i in range(0, len(unsent), BATCH):
                chunk = unsent[i:i + BATCH]
                entries = [{"user": e["user"], "type": e["type"], "time": e["time"],
                            "data": json.dumps(e["details"], sort_keys=True)[:MAX_TEXT]} for e in chunk]
                self._comm.sendMessage("LOG_BATCH", {"entries": entries})
                for e in chunk:
                    e["sent"] = True
            if unsent:
                self._save()
            return len(unsent)

    def _save(self):
        self._nvram.write("activity_log", self._events)
