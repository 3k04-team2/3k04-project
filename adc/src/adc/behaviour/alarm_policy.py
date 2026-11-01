"""1.3.3 Alarm Policy.

Secret: which conditions sound the alarm and who may silence it ("REQ for
alarm (c_alarmSound)"). Implements FR-SEC-05..07 and FR-HMS-05: every alarm and
every disarm is logged with its reason, the time to disarm and optional notes,
and sent to the HMS.

An alarm stays Sounding until an administrator or technician disarms it.
"""

import threading

REASONS = {
    "FORCED_ENTRY": "cabinet forced open",
    "CABINET_DAMAGE": "cabinet damaged (tamper switch)",
    "DOOR_OPEN_TOO_LONG": "door open for more than 60 s",
    "NOT_RETRIEVED": "medication not retrieved within 60 s of the request",
    "INVALID_STATE": "invalid cabinet state",
}
DISARM_ROLES = ("Administrator", "Technician")
MAX_NOTES = 200


class NotAllowed(Exception):
    pass


class AlarmPolicy:
    def __init__(self, alarm_hw, timer, logger, comm):
        self._hw = alarm_hw
        self._timer = timer
        self._logger = logger
        self._comm = comm
        self._lock = threading.RLock()
        self._active = {}

    def reportCondition(self, data):
        """data: {"condition": one of REASONS, "text": optional}. Returns True if a new alarm sounded."""
        reason = data["condition"]
        text = data.get("text") or REASONS.get(reason, reason)
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

    def acknowledgeAlarm(self, userID, role, notes=""):
        """Disarm every active alarm. Only administrators and technicians may (FR-SEC-07)."""
        if role not in DISARM_ROLES:
            raise NotAllowed("only an administrator or technician can disarm the alarm")
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
            self._logger.flushLog(self._comm)
        return [a["reason"] for a in cleared]

    def resetAlarms(self):
        """Forget every alarm without logging (the controller is losing power)."""
        with self._lock:
            for a in self._active.values():
                self._timer.cancelTiming(a["timing"])
            self._active = {}
            self._hw.alarmOff()
