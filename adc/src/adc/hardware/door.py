"""1.1.4 Door.

Secret: the door solenoid, the door contact switch (i_doorSensor), the latch
sensor (i_lockSensor) and the enclosure tamper switch (i_tamperSensor).
Sensor changes are pushed to registered handlers (interrupts, not polling) as
"opened", "closed" or "tamper". A latched door does not open (NAT-01); forcing
it open or damaging the cabinet trips the tamper switch.

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
        """callback(event) with event "opened", "closed" or "tamper"."""
        self._handlers.append(callback)

    def clearHandlers(self):
        """The controller lost power: nothing is listening to the sensors."""
        self._handlers = []

    # ---- the physical world -----------------------------------------------------
    def setOpen(self, is_open):
        """Returns False if the door stayed shut because the latch is engaged."""
        with self._lock:
            if is_open == self._open:
                return True
            if is_open and self._locked:
                return False
            self._open = is_open
        self._notify("opened" if is_open else "closed")
        return True

    def setTamper(self, on):
        with self._lock:
            changed = on != self._tamper
            self._tamper = on
        if changed and on:
            self._notify("tamper")

    def _notify(self, event):
        for h in list(self._handlers):
            h(event)
