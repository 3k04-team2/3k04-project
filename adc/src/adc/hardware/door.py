"""1.1.4 Door.

Secret: the door solenoid, the door contact switch (i_doorSensor), the latch
sensor (i_lockSensor) and the enclosure tamper switch (i_tamperSensor).
Sensor changes are pushed to registered handlers (interrupts, not polling) as
"opened", "closed", "forced" (opened while the latch was engaged, NAT-01) or
"tamper".

setOpen and setTamper stand in for the physical world; the web panel calls them.
"""

import threading


class Door:
    def __init__(self):
        self._open = False
        self._locked = True
        self._tamper = False
        self._handlers = []
        self._lock = threading.Lock()

    # ---- exported operations ------------------------------------------------
    def lockDoor(self):
        with self._lock:
            self._locked = True

    def unlockDoor(self):
        with self._lock:
            self._locked = False

    def isDoorClosed(self):
        return not self._open

    def isLocked(self):
        return self._locked

    def isTampered(self):
        return self._tamper

    def registerDoorSensorHandler(self, callback):
        """callback(event) with event "opened", "closed", "forced" or "tamper"."""
        self._handlers.append(callback)

    def clearHandlers(self):
        """The controller lost power: nothing is listening to the sensors."""
        self._handlers = []

    # ---- the physical world -----------------------------------------------------
    def setOpen(self, is_open):
        with self._lock:
            if is_open == self._open:
                return
            self._open = is_open
            forced = is_open and self._locked
        self._notify("forced" if forced else "opened" if is_open else "closed")

    def setTamper(self, on):
        with self._lock:
            changed = on != self._tamper
            self._tamper = on
        if changed and on:
            self._notify("tamper")

    def _notify(self, event):
        for h in list(self._handlers):
            h(event)
