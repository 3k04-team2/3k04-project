import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "monitor", "src"))   # the HMS, for the round-trip test
sys.path.insert(0, os.path.join(HERE, "..", "src"))                    # the cabinet

from adc.behaviour import state_machine as sm                          # noqa: E402
from adc.cabinet import Cabinet                                        # noqa: E402
from adc.hardware import Nvram, SimulatedTimer                         # noqa: E402
from adc.software import HmsComm                                       # noqa: E402
from adc.ui import Panel                                               # noqa: E402

USERS = {"doctor1": ("pw", "Doctor"), "nurse1": ("pw", "Nurse"), "pharmacist1": ("pw", "Pharmacist"),
         "admin1": ("pw", "Administrator"), "tech1": ("pw", "Technician")}
ORDERS = [{"order": "ORD-1", "compartment": 0, "medication": "Insulin", "quantity": 5},
          {"order": "ORD-2", "compartment": 1, "medication": "Morphine", "quantity": 2}]


def wait_for(cond, seconds=8.0):
    end = time.time() + seconds
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


class FakeComm:
    """Stands in for the HMS link: checks passwords from USERS and records what is sent."""

    def __init__(self):
        self.sent = []
        self.connected = True
        self.order_handler = None

    def checkAuth(self, user, password):
        if not self.connected:
            return "unreachable", None
        rec = USERS.get(user)
        return ("ok", rec[1]) if rec and rec[0] == password else ("denied", None)

    def sendMessage(self, type_, body):
        self.sent.append((type_, body))
        return "sent" if self.connected else "queued"

    def registerNewOrderHandler(self, handler):
        self.order_handler = handler

    def isConnected(self):
        return self.connected

    def getQueuedCount(self):
        return 0

    def start(self):
        pass

    def stop(self):
        pass

    def of(self, type_):
        return [b for t, b in self.sent if t == type_]


class CabinetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.timer = SimulatedTimer(start=1_700_000_000.0)
        self.comm = FakeComm()
        self.cab = self.make()
        self.comm.order_handler(ORDERS)

    def tearDown(self):
        self.cab.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make(self, capacity=4):
        cab = Cabinet("ADC-1", capacity=capacity, data_dir=self.tmp, timer=self.timer,
                      comm_factory=lambda nvram: self.comm)
        cab.power_on(run=False)
        return cab

    # helpers
    def press(self, kind, **data):
        return self.cab.press(kind, **data)

    def state(self):
        return self.cab.machine.getState()

    def tick(self, seconds):
        self.timer.advance(seconds)
        self.cab.dispensing.handleTimeout()

    def login(self, user):
        r = self.press("login", user=user, password="pw")
        self.assertTrue(r["ok"], r)

    def deposit(self, order="ORD-1", logout=True):
        self.login("pharmacist1")
        self.assertTrue(self.press("scan_deposit", order=order)["ok"])
        self.assertTrue(self.press("confirm_quantity", matches=True)["ok"])
        self.cab.set_door_open(True)
        self.assertTrue(self.press("compartment_done")["ok"])
        self.cab.set_door_open(False)
        if logout:
            self.assertTrue(self.press("logout")["ok"])

    # tests
    def test_deposit_then_dispense(self):
        self.deposit(logout=False)
        self.assertEqual(self.state(), sm.AUTHORIZED)                      # still logged in
        self.assertEqual(self.cab.auth.getCurrentUser(), "pharmacist1")
        self.assertTrue(self.cab.door.isLocked())
        self.assertEqual(self.cab.inventory.getCompartment("ORD-1"), 0)
        self.assertEqual(self.comm.of("ORDER_STATUS")[0]["orders"], [{"order": "ORD-1", "status": "Stocked"}])
        ev = self.comm.of("EVENT")[0]
        self.assertEqual((ev["kind"], ev["order"], ev["medication"], ev["quantity"]), ("deposit", "ORD-1", "Insulin", 5))
        rec = self.cab.logger.getEvents(filter="DEPOSIT")[0]                # FR-HMS-01
        self.assertEqual((rec["user"], rec["details"]["order"], rec["details"]["medication"],
                          rec["details"]["quantity"]), ("pharmacist1", "ORD-1", "Insulin", 5))
        self.assertTrue(rec["time"])
        self.assertTrue(self.comm.of("LOG_BATCH"))
        self.assertTrue(self.press("logout")["ok"])

        self.login("nurse1")
        self.assertFalse(self.press("dispense")["ok"])                  # must be shown first (FR-DSP-02)
        r = self.press("select_order", order="ORD-1")
        self.assertIn("Insulin x5", r["message"])
        self.assertTrue(self.press("dispense")["ok"])
        self.assertEqual(self.state(), sm.DISPENSE)
        self.assertFalse(self.cab.door.isLocked())
        self.cab.set_door_open(True)
        self.assertEqual(self.state(), sm.DOOR_OPEN)
        self.press("compartment_done")
        self.cab.set_door_open(False)
        self.assertEqual(self.state(), sm.AUTHORIZED)
        self.assertTrue(self.cab.door.isLocked())
        self.assertEqual(self.cab.inventory.getOrderStatus("ORD-1"), "Fulfilled")
        self.assertEqual(self.comm.of("ORDER_STATUS")[-1]["orders"], [{"order": "ORD-1", "status": "Fulfilled"}])
        self.assertEqual(self.cab.auth.getCurrentUser(), "nurse1")
        self.assertTrue(self.press("logout")["ok"])
        self.assertEqual(self.state(), sm.IDLE)
        self.assertFalse(self.cab.auth.isSessionActive())

    def test_pharmacist_cannot_dispense_and_nurse_cannot_deposit(self):
        self.login("pharmacist1")
        self.assertFalse(self.press("select_order", order="ORD-1")["ok"])
        self.press("logout")
        self.login("nurse1")
        self.assertFalse(self.press("scan_deposit", order="ORD-1")["ok"])

    def test_relock_after_10_seconds_unopened(self):
        self.login("pharmacist1")
        self.press("scan_deposit", order="ORD-1")
        self.press("confirm_quantity", matches=True)
        self.tick(9.9)
        self.assertEqual(self.state(), sm.DEPOSIT)
        self.tick(0.2)
        self.assertEqual(self.state(), sm.AUTHORIZED)                      # relocked, still logged in
        self.assertTrue(self.cab.door.isLocked())
        self.assertEqual(self.cab.inventory.getOrderStatus("ORD-1"), "Pending")

    def test_door_open_too_long_goes_invalid_and_alarms(self):
        self.login("pharmacist1")
        self.press("scan_deposit", order="ORD-1")
        self.press("confirm_quantity", matches=True)
        self.cab.set_door_open(True)
        self.tick(60.0)
        self.assertEqual(self.state(), sm.DOOR_OPEN)
        self.tick(0.1)
        self.assertEqual(self.state(), sm.INVALID)
        self.assertTrue(self.cab.alarm_hw.isSounding())
        self.assertEqual(self.comm.of("ALARM")[0]["alarm"], "DOOR_OPEN_TOO_LONG")
        self.assertFalse(self.press("reset", user="tech1", password="pw")["ok"])      # door still open
        self.cab.set_door_open(False)
        self.assertFalse(self.press("disarm", user="nurse1", password="pw")["ok"])    # FR-SEC-07
        self.assertTrue(self.press("reset", user="tech1", password="pw")["ok"])
        self.assertEqual(self.state(), sm.IDLE)
        self.tick(20)
        self.assertTrue(self.press("disarm", user="admin1", password="pw", notes="door propped open")["ok"])
        self.assertFalse(self.cab.alarm_hw.isSounding())
        self.assertEqual(self.comm.of("ALARM_CLEARED")[0]["alarm"], "DOOR_OPEN_TOO_LONG")
        disarm = self.cab.logger.getEvents(filter="ALARM_DISARMED")[0]
        self.assertEqual(disarm["user"], "admin1")
        self.assertEqual(disarm["details"]["notes"], "door propped open")
        self.assertAlmostEqual(disarm["details"]["time_to_disarm_s"], 20.0, delta=0.5)

    def test_lockout_on_fifth_failure_and_reset(self):
        for i in range(1, 5):
            r = self.press("login", user="nurse1", password="wrong")
            self.assertFalse(r["ok"])
            self.assertEqual(bool(r.get("warning")), i >= 3)                # NFR-SEC-01
        self.assertEqual(self.state(), sm.IDLE)
        r = self.press("login", user="nurse1", password="wrong")
        self.assertTrue(r.get("lockedOut"))
        self.assertEqual(self.state(), sm.LOCKED_OUT)
        self.assertFalse(self.press("login", user="nurse1", password="pw")["ok"])
        self.assertFalse(self.press("dismiss")["ok"])
        self.assertFalse(self.press("reset", user="doctor1", password="pw")["ok"])    # FR-SEC-04
        self.assertTrue(self.press("reset", user="admin1", password="pw")["ok"])
        self.assertEqual(self.state(), sm.IDLE)
        self.login("nurse1")

    def test_forced_entry_and_tamper_lock_out_with_alarm(self):
        self.cab.set_door_open(True)                                        # latched door forced open
        self.assertEqual(self.state(), sm.LOCKED_OUT)
        self.assertTrue(self.cab.alarm_hw.isSounding())
        self.assertEqual(self.comm.of("ALARM")[0]["alarm"], "FORCED_ENTRY")
        self.cab.set_door_open(False)
        self.press("reset", user="tech1", password="pw")
        self.cab.set_tamper(True)
        self.assertEqual(self.state(), sm.LOCKED_OUT)
        self.assertIn("CABINET_DAMAGE", [b["alarm"] for b in self.comm.of("ALARM")])
        self.assertFalse(self.press("reset", user="tech1", password="pw")["ok"])      # still tampered
        self.cab.set_tamper(False)
        self.assertTrue(self.press("reset", user="tech1", password="pw")["ok"])

    def test_deposit_blocks(self):
        self.login("pharmacist1")
        r = self.press("scan_deposit", order="ORD-1", destination="ADC-9")        # FR-IVT-02
        self.assertEqual(r["blocked"], "wrong ADC")
        self.assertFalse(self.press("scan_deposit", order="ORD-1")["ok"])          # modal must be dismissed
        self.press("dismiss")
        self.assertEqual(self.press("scan_deposit", order="ORD-77")["blocked"], "not on order list")
        self.press("dismiss")
        self.press("scan_deposit", order="ORD-1")
        self.assertEqual(self.press("confirm_quantity", matches=False)["blocked"], "quantity not confirmed")
        self.assertEqual(self.state(), sm.AUTHORIZED)
        self.assertEqual(self.cab.inventory.getOrderStatus("ORD-1"), "Pending")
        with self.assertRaises(ValueError):                                       # FR-IVT-04 invariant
            self.cab.inventory.recordDeposit("ORD-1", 0)

    def test_full_cabinet_blocks_deposit(self):
        self.cab.stop()
        self.cab = self.make(capacity=1)
        self.deposit("ORD-1")
        self.login("pharmacist1")
        self.assertEqual(self.press("scan_deposit", order="ORD-2")["blocked"], "cabinet full")   # FR-IVT-03
        self.assertTrue(self.comm.of("INVENTORY")[-1]["capacity_warning"])                         # FR-HMS-04

    def test_invalid_order_id_is_refused(self):
        self.deposit("ORD-2")
        self.login("doctor1")
        r = self.press("select_order", order="ORD-1")                     # not stocked yet
        self.assertFalse(r["ok"])
        self.assertEqual(self.state(), sm.AUTHORIZED)
        self.assertFalse(self.cab.alarm_hw.isSounding())
        self.assertFalse(self.press("dispense")["ok"])
        self.assertTrue(self.press("select_order", order="ORD-2")["ok"])  # can retry right away

    def test_inventory_and_lockout_survive_power_loss(self):
        self.deposit()
        for _ in range(5):
            self.press("login", user="nurse1", password="wrong")
        self.cab.power_off()
        self.assertTrue(self.cab.press("login", user="nurse1", password="pw")["ok"] is False)
        self.cab.power_on(run=False)
        self.assertEqual(self.cab.inventory.listInventory(), [("ORD-1", 0)])        # FR-HMS-06
        self.assertEqual(self.state(), sm.LOCKED_OUT)
        self.assertTrue(self.cab.logger.getEvents(filter="DEPOSIT"))

    def test_session_times_out_after_5_minutes(self):
        self.login("nurse1")
        self.tick(299)
        self.assertEqual(self.state(), sm.AUTHORIZED)
        self.tick(2)
        self.assertEqual(self.state(), sm.IDLE)
        self.assertFalse(self.cab.auth.isSessionActive())

    def test_any_interaction_restarts_the_5_minute_timer(self):
        self.login("nurse1")
        self.tick(200)
        self.press("select_order", order="ORD-404")                        # even a refused input counts
        self.tick(200)
        self.assertEqual(self.state(), sm.AUTHORIZED)
        self.assertGreater(self.cab.snapshot()["session_left"], 90)
        self.press("cancel")
        self.tick(299)
        self.assertEqual(self.state(), sm.AUTHORIZED)
        self.tick(2)
        self.assertEqual(self.state(), sm.IDLE)

    def test_hms_unreachable_refuses_without_counting(self):
        self.comm.connected = False
        r = self.press("login", user="nurse1", password="pw")
        self.assertFalse(r["ok"])
        self.assertIn("HMS", r["message"])
        self.assertEqual(self.cab.auth.failures(), 0)
        self.assertEqual(self.state(), sm.IDLE)

    def test_reset_while_hms_unreachable_says_so(self):
        self.cab.set_tamper(True)
        self.cab.set_tamper(False)
        self.comm.connected = False
        r = self.press("reset", user="tech1", password="pw")
        self.assertFalse(r["ok"])
        self.assertIn("Cannot reach the HMS", r["message"])
        self.assertEqual(self.state(), sm.LOCKED_OUT)
        self.comm.connected = True
        self.assertTrue(self.press("reset", user="tech1", password="pw")["ok"])

    def test_admin_cannot_open_cabinet(self):
        r = self.press("login", user="admin1", password="pw")
        self.assertFalse(r["ok"])
        self.assertEqual(self.state(), sm.IDLE)

    def test_order_cancelled_by_hms(self):
        self.comm.order_handler(ORDERS[1:])
        self.assertEqual(self.cab.inventory.getOrderStatus("ORD-1"), "Cancelled")

    def test_panel_api(self):
        panel = Panel(self.cab)
        panel.start(0)
        try:
            def post(path, body):
                req = urllib.request.Request("http://127.0.0.1:%d%s" % (panel.port, path),
                                             json.dumps(body).encode(), {"Content-Type": "application/json"})
                return json.loads(urllib.request.urlopen(req).read())
            page = urllib.request.urlopen("http://127.0.0.1:%d/" % panel.port).read()
            self.assertIn(b"Automated Dispensing Cabinet", page)
            out = post("/api/input", {"kind": "login", "user": "nurse1", "password": "pw"})
            self.assertTrue(out["result"]["ok"])
            self.assertEqual(out["state"]["state"], sm.AUTHORIZED)
            out = post("/api/power", {"on": False})
            self.assertFalse(out["state"]["powered"])
        finally:
            panel.stop()


class HmsCommTests(unittest.TestCase):
    def test_offline_messages_are_queued_and_kept(self):
        tmp = tempfile.mkdtemp()
        try:
            nv = Nvram(tmp)
            comm = HmsComm("ADC-1", "127.0.0.1", 1, 4, nv, log=lambda t: None)
            self.assertEqual(comm.sendMessage("EVENT", {"kind": "deposit"}), "queued")
            self.assertEqual(comm.checkAuth("nurse1", "x"), ("unreachable", None))
            again = HmsComm("ADC-1", "127.0.0.1", 1, 4, Nvram(tmp), log=lambda t: None)
            self.assertEqual(again.getQueuedCount(), 1)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class HmsRoundTripTests(unittest.TestCase):
    """The cabinet against the real HMS: login checked by the HMS, order synced, deposit, withdraw."""

    def setUp(self):
        from hms.host import SimulatedHost
        from main import System
        self.tmp = tempfile.mkdtemp()
        self.host = SimulatedHost(start=time.time() - 7200)       # HMS clock 2 h off, as in a --sim-host demo
        self.sys = System(os.path.join(self.tmp, "hms"), self.host, adc_port=0, ui_port=0)
        self.sys.start()
        self.cab = Cabinet("ADC-7", "127.0.0.1", self.sys.adc_port, 8, os.path.join(self.tmp, "adc"),
                           timer=SimulatedTimer())
        self.cab.power_on()
        self.assertTrue(wait_for(lambda: self.sys.health.state("ADC-7") == "Online"))

    def tearDown(self):
        self.cab.stop()
        self.sys.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_full_cycle(self):
        from hms.access import demo_password
        status = lambda oid: [o["status"] for o in self.sys.orders.orders() if o["id"] == oid][0]
        doc = self.sys.access.login("doctor1", demo_password("doctor1"), "dev-doc")
        oid = self.sys.orders.submit(doc, "ADC-7", "Insulin", 5, True)
        self.assertTrue(wait_for(lambda: self.cab.inventory.getOrderStatus(oid) == "Pending"))

        r = self.cab.press("login", user="pharmacist1", password="wrong")
        self.assertEqual(r["access"], "denied")
        self.assertEqual(self.cab.auth.failures(), 1)
        r = self.cab.press("login", user="pharmacist1", password=demo_password("pharmacist1"))
        self.assertTrue(r["ok"], r)
        self.cab.press("scan_deposit", order=oid)
        self.cab.press("confirm_quantity", matches=True)
        self.cab.set_door_open(True)
        self.cab.press("compartment_done")
        self.cab.set_door_open(False)
        self.assertTrue(wait_for(lambda: status(oid) == "Stocked"))
        self.assertTrue(self.cab.press("logout")["ok"])

        r = self.cab.press("login", user="nurse1", password=demo_password("nurse1"))
        self.assertTrue(r["ok"], r)
        self.cab.press("select_order", order=oid)
        self.cab.press("dispense")
        self.cab.set_door_open(True)
        self.cab.press("compartment_done")
        self.cab.set_door_open(False)
        self.assertTrue(wait_for(lambda: status(oid) == "Fulfilled"))
        self.assertTrue(wait_for(lambda: self.cab.comm.getQueuedCount() == 0))
        types = {e["type"] for e in self.sys.log.query({})}
        self.assertTrue({"ADC_LOGIN", "ADC_LOGIN_FAILED", "ADC_WITHDRAW", "ADC_DEPOSIT"} <= types, types)
        self.assertEqual(self.sys.log.verify(), [])


if __name__ == "__main__":
    unittest.main()
