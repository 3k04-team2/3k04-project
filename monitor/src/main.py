"""Hospital Monitoring System (HMS): starts every module and connects them.

Run:   py main.py                       real host, ADC port 9000, web page on port 8080
       py main.py --sim-host            simulated clock and speaker (adds demo buttons)
Open http://127.0.0.1:8080 and log in as doctor1 / doctor1!demo (see README.md).
"""

import argparse
import os
import threading
import time

from hms.access import AccessControl
from hms.alarms import AlarmManager
from hms.gateway import AdcGateway
from hms.health import HealthMonitor
from hms.host import HostPlatform, SimulatedHost
from hms.inventory import InventoryTracker
from hms.link import AdcLink
from hms.orders import OrderManager
from hms.protocol import Message, MessageCodec
from hms.store import LogStore, StateStore
from hms.ui import WebApp
from hms.ui.app import PAGE

TICK = 0.1


class DemoHost(SimulatedHost):
    """The simulated clock, speaker and failures, but files really go to the disk.

    SimulatedHost keeps files in memory (right for tests). A demo needs the exported
    CSV and the saved state to exist on the disk, so every write is also made for real
    and reads fall back to the disk.
    """

    def __init__(self, start=None):
        SimulatedHost.__init__(self, start if start is not None else time.time())
        self._real = HostPlatform()

    def write_file(self, path, data):
        SimulatedHost.write_file(self, path, data)      # raises DiskError when "disk full"
        self._real.write_file(path, data)

    def copy_file(self, src, dst):
        if src in self.files:
            SimulatedHost.copy_file(self, src, dst)     # raises DiskError when "disk full"
        self._real.copy_file(src, dst)

    def exists(self, path):
        return path in self.files or self._real.exists(path)

    def read_file(self, path):
        return self.files[path] if path in self.files else self._real.read_file(path)


class System:
    """All twelve modules, wired in the order of the uses-hierarchy."""

    def __init__(self, data_dir, host=None, adc_port=9000, ui_port=8080, bind="127.0.0.1"):
        self.host = host or HostPlatform()
        self.sim = self.host if isinstance(self.host, SimulatedHost) else None
        if self.sim is not None:
            self.sim.files[PAGE] = open(PAGE, "rb").read()      # the page is read through M12
        self.data_dir = data_dir
        self.adc_port, self.ui_port, self.bind = adc_port, ui_port, bind

        self.log = LogStore(self.host, data_dir)
        self.state = StateStore(self.host, data_dir)
        self.link = AdcLink()
        self.codec = MessageCodec()
        self.gateway = AdcGateway(self.link, self.codec, self.log, self.host)
        self.access = AccessControl(self.gateway, self.log, self.state, self.host)
        self.alarms = AlarmManager(self.access, self.log, self.host)
        self.health = HealthMonitor(self.gateway, self.alarms, self.log, self.state, self.host,
                                    authorize=self.access.allowed)
        self.inventory = InventoryTracker(
            self.alarms, self.log, self.state,
            is_online=lambda adc: self.health.state(adc) == "Online")
        self.orders = OrderManager(self.gateway, self.health, self.inventory, self.alarms,
                                   self.access, self.log, self.state, clock=self.host.now)
        self.ui = WebApp(self.host, self.health, self.orders, self.inventory, self.alarms,
                         self.access, self.log, sim=self.sim)
        self._wire()
        self._stop = threading.Event()
        self._thread = None
        self.recovery_notes = []

    # ---- connecting the modules to the ADC messages -------------------------------
    def _wire(self):
        g = self.gateway
        g.subscribe("HELLO", self.health.onHello)
        g.subscribe("HELLO", self.inventory.onHello)
        g.subscribe("HELLO", self._after_hello)
        g.subscribe("HEARTBEAT", self.health.onHeartbeat)
        g.subscribe("ALARM", self._on_alarm)
        g.subscribe("ALARM_CLEARED", lambda adc, m: self.alarms.clear(adc, m.body.get("alarm", "")))
        g.subscribe("EVENT", self.inventory.onEvent)
        g.subscribe("INVENTORY", self.inventory.onInventory)
        g.subscribe("ORDER_STATUS", self.orders.onOrderStatus)
        g.subscribe("AUTH_REQUEST", self._on_auth_request)
        self.health.onStateChange(self.orders.onAdcStateChange)

    def _on_auth_request(self, adc, msg):
        # A login at the ADC (Part A checks every login through the HMS). The answer
        # is sent once, without waiting for an ACK, so the reader thread never blocks.
        b = msg.body
        user = b.get("user") if isinstance(b.get("user"), str) else ""
        password = b.get("password") if isinstance(b.get("password"), str) else ""
        granted, role = self.access.checkAdcLogin(adc, user, password)
        self.gateway.answer(adc, Message("AUTH_RESULT", body={
            "request": msg.seq, "user": user,
            "result": "granted" if granted else "denied", "role": role}))

    def _on_alarm(self, adc, msg):
        b = msg.body
        self.alarms.raise_(b.get("alarm", "ADC_ALARM"), b.get("severity", "warning"), adc,
                           b.get("text", ""))
        self.health.onAlarm(adc, msg)

    def _after_hello(self, adc, msg):
        # The ADC link's reader thread must not wait for an ACK, so resend in a thread.
        def work():
            self.orders.resync(adc)
            try:
                self.gateway.send(adc, self.access.roles_message())
            except Exception as e:
                self.log.append({"adc": adc, "type": "ROLE_SYNC_FAILED", "data": repr(e)})
        threading.Thread(target=work, daemon=True).start()

    # ---- start and stop -------------------------------------------------------------
    def start(self):
        self._recover()
        self.link.start(self.adc_port, self.bind)
        self.adc_port = self.link.port or self.adc_port
        self.ui.start(self.ui_port, self.bind)
        self.ui_port = self.ui.port
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(2)
        self.ui.stop()
        self.link.stop()

    def _run(self):
        last = time.monotonic()
        while not self._stop.wait(TICK):
            real = time.monotonic()
            if self.sim is not None:
                self.sim.advance(real - last)       # simulated time follows real time
            last = real
            try:
                self.health.tick(self.host.monotonic())
            except Exception as e:                  # a failing tick must be seen, not hidden
                print("tick error:", repr(e))

    # ---- start-up recovery (SPEC-09, SPEC-11) --------------------------------------------
    def _recover(self):
        for name, store in (("state", self.state), ("log", self.log)):
            if store.damaged:
                try:
                    age = store.restoreLatest()
                    note = "the %s file was damaged; restored from a backup %.0f hours old" % (name, age / 3600)
                    severity = "warning"
                except Exception as e:                # no backup to restore: say so loudly
                    note = "the %s file was damaged and could not be restored: %r" % (name, e)
                    severity = "critical"
                self.recovery_notes.append(note)
                self.alarms.raise_("STORE_RECOVERED", severity, "", note)
        bad = self.log.verify()
        if bad:
            self.alarms.raise_("LOG_TAMPERED", "critical", "",
                               "log entries changed after they were written: %s" % bad[:10])


def main():
    ap = argparse.ArgumentParser(description="Hospital Monitoring System")
    ap.add_argument("--port", type=int, default=9000, help="port the ADCs connect to")
    ap.add_argument("--ui-port", type=int, default=8080, help="port of the web page")
    ap.add_argument("--data", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
    ap.add_argument("--sim-host", action="store_true", help="simulated clock, speaker and disk")
    ap.add_argument("--lan", action="store_true", help="listen on all network interfaces")
    a = ap.parse_args()
    os.makedirs(a.data, exist_ok=True)
    host = DemoHost() if a.sim_host else None
    system = System(a.data, host, a.port, a.ui_port, "0.0.0.0" if a.lan else "127.0.0.1")
    system.start()
    print("HMS running. ADCs connect to port %d. Web page: http://127.0.0.1:%d" % (system.adc_port, system.ui_port))
    for n in system.recovery_notes:
        print("recovery:", n)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        system.stop()


if __name__ == "__main__":
    main()
