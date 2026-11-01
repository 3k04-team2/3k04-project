"""M7 Alarm Manager.

Secret: alarm severities, who is told about which alarm, and how notifications
are delivered (on screen now; pager or email later). Implements SPEC-03.
Uses: M8 Access Control, M9 Log Store, M12 Host Platform.

Python reserves the word "raise", so the operation is raise_ (also reachable as
getattr(manager, "raise")).
"""

import threading

from hms.errors import NotAllowed
from hms.host import SpeakerUnavailable


class Alarm:
    def __init__(self, id_, type_, severity, adc, text, time):
        self.id = id_
        self.type = type_
        self.severity = severity
        self.adc = adc
        self.text = text
        self.time = time
        self.status = "Active"       # Active -> Acknowledged or Cleared
        self.by = ""

    def as_dict(self):
        return {"id": self.id, "type": self.type, "severity": self.severity, "adc": self.adc,
                "text": self.text, "time": self.time, "status": self.status, "by": self.by}


class AlarmManager:
    def __init__(self, access, log, host):
        self._access = access
        self._log = log
        self._host = host
        self._lock = threading.RLock()
        self._alarms = []
        self._notices = []
        self._n = 0
        self._speaker_fault = False

    def raise_(self, type, severity, adcId, text):
        with self._lock:
            self._n += 1
            alarm = Alarm("AL-%d" % self._n, type, severity, adcId, text, self._host.now())
            self._alarms.append(alarm)
            self._log.append({"adc": adcId, "type": "ALARM", "data": "%s %s: %s" % (severity, type, text)})
        self._update_tone()
        return alarm.id

    def acknowledge(self, session, alarmId):
        if not self._access.allowed(session, "ack_alarm"):
            self._log.append({"user": session.user, "type": "DENIED", "data": "ack_alarm"})
            raise NotAllowed("ack_alarm")
        with self._lock:
            for a in self._alarms:
                if a.id == alarmId and a.status == "Active":
                    a.status = "Acknowledged"
                    a.by = session.user
                    self._log.append({"adc": a.adc, "user": session.user, "type": "ALARM_ACK", "data": a.id})
        self._update_tone()

    def clear(self, adcId, alarm_type):
        with self._lock:
            for a in self._alarms:
                if a.adc == adcId and a.type == alarm_type and a.status != "Cleared":
                    a.status = "Cleared"
                    self._log.append({"adc": adcId, "type": "ALARM_CLEARED", "data": a.id})
        self._update_tone()

    def active(self):
        with self._lock:
            return [a.as_dict() for a in self._alarms if a.status == "Active"]

    def recent(self, limit=50):
        with self._lock:
            return [a.as_dict() for a in self._alarms[-limit:]][::-1]

    def toneOn(self):
        with self._lock:
            return any(a.severity == "critical" and a.status == "Active" for a in self._alarms)

    def notify(self, role_or_user, text):
        with self._lock:
            self._notices.append({"time": self._host.now(), "to": role_or_user, "text": text})
            del self._notices[:-200]
        self._log.append({"user": role_or_user, "type": "NOTICE", "data": text})

    def notices_for(self, user, role):
        with self._lock:
            return [n for n in self._notices if n["to"] in (user, role)][-30:][::-1]

    def health(self):
        return True

    def _update_tone(self):
        want = self.toneOn()
        try:
            self._host.set_tone(want)
            self._speaker_fault = False
        except SpeakerUnavailable:
            # the alarm is still shown on screen; the broken speaker is its own alarm
            if not self._speaker_fault:
                self._speaker_fault = True
                self.raise_("SPEAKER_FAULT", "warning", "", "the audible alarm cannot sound")


setattr(AlarmManager, "raise", AlarmManager.raise_)
