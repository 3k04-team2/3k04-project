"""1 ADC Entire System: builds every module and connects them.

The door (the physical enclosure) and the memory outlive a power
cut; everything else is rebuilt from storage when the power comes back
(NAT-05, FR-HMS-06).
"""

import threading

from adc.behaviour import AlarmPolicy, DispensingManager, StateMachine, state_machine as sm
from adc.hardware import AlarmHardware, Display, Door, Keyboard, Memory, Timer
from adc.software import AuthManager, EventLogger, HmsComm, InventoryManager

TICK = 0.05     # the 500 ms alarm deadline (NFR-SEC-04) is met with a 50 ms tick


class Cabinet:
    def __init__(self, adc_id="ADC-1", hms_host="127.0.0.1", hms_port=9000, capacity=16,
                 data_dir="data", timer=None, comm_factory=None):
        self.id = adc_id
        self.capacity = capacity
        self.door = Door()
        self.memory = Memory(data_dir)
        self.timer = timer or Timer()
        self._comm_factory = comm_factory or (lambda memory: HmsComm(adc_id, hms_host, hms_port, capacity, memory))
        self.powered = False
        self._network = True
        self._stop = None
        self._thread = None
        self._lock = threading.RLock()

    # ---- power ---------------------------------------------------------------------
    def power_on(self, run=True):
        with self._lock:
            if self.powered:
                return
            # 1.1 hardware hiding
            self.alarm_hw = AlarmHardware()
            self.keyboard = Keyboard()
            self.display = Display(self.timer)
            # 1.2 software hiding (uses hardware only)
            self.comm = self._comm_factory(self.memory)
            self.logger = EventLogger(self.memory, self.timer, self.comm)
            self.inventory = InventoryManager(self.id, self.memory, self.capacity, self.comm)
            self.auth = AuthManager(self.comm, self.timer, self.memory, self.logger)
            # 1.3 behaviour (uses software and hardware)
            self.alarms = AlarmPolicy(self.alarm_hw, self.timer, self.logger, self.comm, self.door)
            self.machine = StateMachine(self.timer, self.door, self.auth, self.alarms, self.display, self.logger)
            self.dispensing = DispensingManager(self.id, self.display, self.door, self.keyboard, self.alarm_hw,
                                                self.alarms, self.machine, self.auth, self.inventory,
                                                self.logger, self.comm)
            self.logger.logEvent("POWER_ON", details={"state": self.machine.getState(),
                                                      "stocked": self.inventory.occupancy()})
            self.powered = True
            if hasattr(self.comm, "setNetworkAvailable"):
                self.comm.setNetworkAvailable(self._network)
            if run:
                self.comm.start()
                self._stop = threading.Event()
                self._thread = threading.Thread(target=self._run, args=(self._stop,), daemon=True)
                self._thread.start()

    def power_off(self):
        """Mains power lost: RAM is gone, the door stays latched (fail-secure)."""
        with self._lock:
            if not self.powered:
                return
            self.powered = False
            if self._stop is not None:
                self._stop.set()
            self.logger.logEvent("POWER_OFF")
            self.comm.stop()
            self.alarms.resetAlarms()
            self.door.clearHandlers()
            self.door.lockDoor()

    def stop(self):
        self.power_off()

    def _run(self, stop):
        while not stop.wait(TICK):
            try:
                self.dispensing.handleTimeout()
            except Exception as e:          # a failing tick must be seen, not hidden
                print("tick error:", repr(e), flush=True)

    # ---- the physical world (driven by the web panel) -----------------------------------
    def press(self, kind, **data):
        if not self.powered:
            return {"ok": False, "message": "The cabinet has no power."}
        return self.keyboard.press(kind, **data)

    def set_door_open(self, is_open):
        if self.door.setOpen(is_open):
            return {"ok": True}
        if not self.powered:
            return {"ok": False, "message": "The cabinet has no power; the door stays latched."}
        return self.dispensing.explainLockedDoor()

    def set_tamper(self, on):
        self.door.setTamper(on)

    def set_network(self, up):
        self._network = up
        if self.powered and hasattr(self.comm, "setNetworkAvailable"):
            self.comm.setNetworkAvailable(up)

    # ---- what the panel shows -----------------------------------------------------------------
    def snapshot(self):
        door = {"closed": self.door.isDoorClosed(), "locked": self.door.isLocked(),
                "tamper": self.door.isTampered()}
        if not self.powered:
            return {"adc": self.id, "powered": False, "door": door, "network": self._network}
        active, left = self.auth.checkSessionTime()
        return {
            "adc": self.id, "powered": True, "network": self._network,
            "state": self.machine.getState(), "maintenance": self.machine.isMaintenance(),
            "user": self.auth.getCurrentUser(), "role": self.auth.getCurrentRole(),
            "session_left": round(left) if active and self.machine.getState() == sm.AUTHORIZED else None,
            "failures": self.auth.failures(), "locked_out": self.auth.isLockedOut(),
            "display": self.display.read(), "door": door,
            "alarm": {"sounding": self.alarm_hw.isSounding(), "suppressed": self.alarm_hw.isSuppressed(),
                      "active": self.alarms.getActiveAlarm()},
            "inventory": {"capacity": self.inventory.capacity(), "occupied": self.inventory.occupancy(),
                          "level": round(self.inventory.level(), 3), "stocked": self.inventory.stocked(),
                          "pending": self.inventory.pending()},
            "hms": {"connected": self.comm.isConnected(), "queued": self.comm.getQueuedCount()},
            "dispense": self.dispensing.getDispenseStatus(),
            "events": [{"time": e["time"], "type": e["type"], "user": e["user"], "details": e["details"]}
                       for e in self.logger.getEvents()[-15:]][::-1],
        }
