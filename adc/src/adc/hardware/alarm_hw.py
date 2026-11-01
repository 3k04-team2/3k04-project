"""1.1.1 Alarm Hardware.

Secret: how the alarm circuit (siren/buzzer) is driven. Simulated here; the web
panel shows the buzzer state.
"""

import threading


class AlarmHardware:
    def __init__(self):
        self._active = False
        self._suppressed = False
        self._lock = threading.Lock()

    def alarmOn(self):
        with self._lock:
            self._active = True

    def alarmOff(self):
        with self._lock:
            self._active = False

    def alarmSuppression(self):
        """Keep the siren quiet while a technician tests the cabinet (maintenance)."""
        with self._lock:
            self._suppressed = True

    def alarmRestore(self):
        with self._lock:
            self._suppressed = False

    def isAlarmActive(self):
        return self._active

    def isSuppressed(self):
        return self._suppressed

    def isSounding(self):
        return self._active and not self._suppressed
