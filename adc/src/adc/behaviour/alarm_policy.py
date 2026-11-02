"""1.3.3 Alarm Policy.

Secret: which conditions sound the alarm ("REQ for alarm (c_alarmSound)").
Implements FR-SEC-06: the tamper switch (cabinet forced open or damaged) sounds
the alarm here. The State Machine reports the door left open too long (FR-SEC-05).
Every alarm and disarm is written to the Event Logger with its reason, time to
disarm and optional notes (FR-HMS-05) and sent to the HMS.

An alarm stays sounding until it is disarmed; who may disarm is decided by
Authentication & Access Control (FR-SEC-07).
"""

import threading

REASONS = {
    "CABINET_DAMAGE": "cabinet forced open or damaged (tamper switch)",
    "DOOR_OPEN_TOO_LONG": "door open for more than 60 s",
}
DOOR_EVENTS = {"tamper": "CABINET_DAMAGE"}
MAX_NOTES = 200


class AlarmPolicy:
    def __init__(self, alarm_hw, timer, logger, comm, door):
        self._hw = alarm_hw
        self._timer = timer
        self._logger = logger
        self._comm = comm
        self._lock = threading.RLock()
        self._active = {}
        door.registerDoorSensorHandler(self._on_door)

    def reportCondition(self, data):
        """data: {"condition": one of REASONS, "text": optional}. Returns True if a new alarm sounded."""
        reason = data["condition"]
        text = data.get("text") or REASONS[reason]
        with self._lock:
            if reason in self._active:
                return False
            self._active[reason] = {"reason": reason, "text": text, "time": self._timer.timeNow(),
                                    "timing": self._timer.startTiming()}
            self._hw.alarmOn()
        self._logger.logEvent("ALARM_RAISED", details={"reason": reason, "text": text})
        self._comm.sendMessage("ALARM", {"alarm": reason, "severity": "critical", "text": text})
        return True

    def getActiveAlarm(self):
        with self._lock:
            return [{"reason": a["reason"], "text": a["text"], "time": a["time"],
                     "elapsed": self._timer.elapsed(a["timing"]) or 0.0} for a in self._active.values()]

    def acknowledgeAlarm(self, userID, notes=""):
        """Disarm every active alarm; the caller has checked the user may (FR-SEC-07)."""
        notes = (notes or "")[:MAX_NOTES]
        with self._lock:
            cleared = list(self._active.values())
            self._active = {}
            self._hw.alarmOff()
        for a in cleared:
            took = self._timer.endTiming(a["timing"]) or 0.0
            self._logger.logEvent("ALARM_DISARMED", user=userID,
                                  details={"reason": a["reason"], "raised_at": a["time"],
                                           "time_to_disarm_s": round(took, 1), "notes": notes})
            self._comm.sendMessage("ALARM_CLEARED", {"alarm": a["reason"]})
        if cleared:
            self._logger.flushLog()
        return [a["reason"] for a in cleared]

    def resetAlarms(self):
        """Forget every alarm without logging (the controller is losing power)."""
        with self._lock:
            for a in self._active.values():
                self._timer.cancelTiming(a["timing"])
            self._active = {}
            self._hw.alarmOff()

    def _on_door(self, event):
        if event in DOOR_EVENTS:
            self.reportCondition({"condition": DOOR_EVENTS[event]})
