import os
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))                  # the monitor code
sys.path.insert(0, HERE)                                             # the cabinet simulator

from hms.host import SimulatedHost                       # noqa: E402
from hms.protocol import (MessageCodec, Message, ChecksumError, FormatError)  # noqa: E402
from hms.store import LogStore                           # noqa: E402
from hms.access import DeviceLocked, BadCredentials, demo_password  # noqa: E402
from hms.orders import OrderError                        # noqa: E402
from main import System                                  # noqa: E402
from adc_sim import Adc                                  # noqa: E402


def wait_for(cond, seconds=5.0):
    end = time.time() + seconds
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


class CodecTests(unittest.TestCase):
    def test_round_trip(self):
        c = MessageCodec()
        m = Message("EVENT", "ADC-1", 7, 12.5, {"medication": "Insulin", "quantity": 3})
        back = c.decode(c.encode(m))
        self.assertEqual((back.type, back.adc, back.seq, back.body), ("EVENT", "ADC-1", 7, m.body))

    def test_corruption_is_refused(self):
        c = MessageCodec()
        data = c.encode(Message("HEARTBEAT", "ADC-1", 1, 1.0, {}))
        with self.assertRaises(ChecksumError):
            c.decode(data.replace(b'"seq":1', b'"seq":2'))
        with self.assertRaises(FormatError):
            c.decode(b"not json")


class LogTests(unittest.TestCase):
    def test_tamper_is_detected(self):
        host = SimulatedHost()
        log = LogStore(host, "d")
        for i in range(5):
            log.append({"adc": "A", "user": "u", "type": "T", "data": "entry %d" % i})
        self.assertEqual(log.verify(), [])
        log._conn.execute("UPDATE log SET hash='00' WHERE id=3")
        self.assertIn(3, log.verify())

    def test_query_filters_and_backup_restore(self):
        host = SimulatedHost()
        log = LogStore(host, "d")
        log.append({"adc": "A", "type": "X", "data": "one"})
        log.append({"adc": "B", "type": "Y", "data": "two"})
        self.assertEqual(len(log.query({"adc": "A"})), 1)
        log.backup()
        log.append({"adc": "A", "type": "X", "data": "three"})
        log.restoreLatest()
        self.assertEqual(len(log.query({})), 2)


class SystemTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.host = SimulatedHost(start=time.time())
        self.sys = System(self.tmp, self.host, adc_port=0, ui_port=0)
        self.sys.start()
        self.adc = Adc("ADC-1", "127.0.0.1", self.sys.adc_port, 8)
        threading.Thread(target=self.adc.run, daemon=True).start()
        self.assertTrue(wait_for(lambda: self.sys.health.state("ADC-1") == "Online"))

    def tearDown(self):
        self.adc.alive = False
        self.sys.stop()

    def login(self, user):
        return self.sys.access.login(user, demo_password(user), "dev-" + user)

    def test_lockout_after_five_failures(self):
        for _ in range(5):
            with self.assertRaises(BadCredentials):
                self.sys.access.login("doctor1", "wrong", "dev-x")
        with self.assertRaises(DeviceLocked):
            self.sys.access.login("doctor1", demo_password("doctor1"), "dev-x")
        self.host.advance(3601)
        self.assertEqual(self.sys.access.login("doctor1", demo_password("doctor1"), "dev-x").user, "doctor1")

    def test_only_consecutive_failures_of_one_user_lock(self):
        for user in ("doctor200", "doctor2", "doctor2", "doctor2", "doctor2", "nurse1"):
            with self.assertRaises(BadCredentials):
                self.sys.access.login(user, "wrong", "dev-y")
        self.assertEqual(self.sys.access.login("doctor1", demo_password("doctor1"), "dev-y").user, "doctor1")
        for _ in range(4):
            with self.assertRaises(BadCredentials):
                self.sys.access.login("nurse1", "wrong", "dev-y")
        self.login("nurse1")                                        # a different device
        with self.assertRaises(BadCredentials):
            self.sys.access.login("nurse1", "wrong", "dev-y")
        with self.assertRaises(DeviceLocked):
            self.sys.access.login("doctor1", demo_password("doctor1"), "dev-y")

    def test_permissions(self):
        nurse = self.login("nurse1")
        self.assertFalse(self.sys.access.allowed(nurse, "submit_order"))
        from hms.errors import NotAllowed
        with self.assertRaises(NotAllowed):
            self.sys.orders.submit(nurse, "ADC-1", "Insulin", 1, True)

    def test_order_flow(self):
        doc = self.login("doctor1")
        with self.assertRaises(OrderError):                     # not confirmed
            self.sys.orders.submit(doc, "ADC-1", "Insulin", 1, False)
        with self.assertRaises(OrderError):                     # above formulary maximum
            self.sys.orders.submit(doc, "ADC-1", "Morphine", 101, True)
        oid = self.sys.orders.submit(doc, "ADC-1", "Insulin", 5, True)
        status = lambda: [o["status"] for o in self.sys.orders.orders() if o["id"] == oid][0]
        self.assertEqual(status(), "Placed")
        self.assertIn(oid, self.adc.orders)
        self.adc.command(["deposit", oid])
        self.assertTrue(wait_for(lambda: status() == "Stocked"))
        self.adc.command(["withdraw", oid])
        self.assertTrue(wait_for(lambda: status() == "Fulfilled"))
        self.assertEqual(self.sys.inventory.free("ADC-1"), 8)
        self.assertEqual(self.sys.log.verify(), [])

    def test_health_thresholds_and_order_at_risk(self):
        doc = self.login("doctor1")
        oid = self.sys.orders.submit(doc, "ADC-1", "Insulin", 5, True)
        self.adc.heartbeat = False
        self.host.advance(2.0)
        self.assertTrue(wait_for(lambda: self.sys.health.state("ADC-1") == "Warning"))
        self.host.advance(2.0)
        self.assertTrue(wait_for(lambda: self.sys.health.state("ADC-1") == "Offline"))
        self.assertTrue(wait_for(lambda: any(a["type"] == "ORDER_AT_RISK" for a in self.sys.alarms.active())))
        self.assertTrue(self.sys.alarms.toneOn())
        self.assertTrue(self.host.tone_is_on())
        with self.assertRaises(OrderError):
            self.sys.orders.submit(doc, "ADC-1", "Insulin", 1, True)
        self.adc.heartbeat = True
        self.assertTrue(wait_for(lambda: self.sys.health.state("ADC-1") == "Online"))
        nurse = self.login("nurse1")
        for a in self.sys.alarms.active():
            self.sys.alarms.acknowledge(nurse, a["id"])
        self.assertFalse(self.host.tone_is_on())
        self.assertIsNotNone(oid)

    def test_adc_login_checked_by_hms(self):
        results = []
        plain = self.adc._handle

        def handle(line):
            msg = MessageCodec().decode(line)
            if msg.type != "AUTH_RESULT":
                return plain(line)
            results.append(msg.body)
            self.adc.send("ACK", {"ack": msg.seq})
        self.adc._handle = handle
        self.adc.send("AUTH_REQUEST", {"request": 1, "user": "nurse1", "password": demo_password("nurse1")})
        self.adc.send("AUTH_REQUEST", {"request": 2, "user": "nurse1", "password": "wrong"})
        self.assertTrue(wait_for(lambda: len(results) == 2))
        by_id = {r["request"]: r for r in results}
        self.assertEqual((by_id[1]["ok"], by_id[1]["role"]), (True, "Nurse"))
        self.assertFalse(by_id[2]["ok"])
        types = [e["type"] for e in self.sys.log.query({})]
        self.assertIn("ADC_LOGIN", types)
        self.assertIn("ADC_LOGIN_FAILED", types)
        # the ADC owns its lockout, so failures there never lock the HMS device
        for _ in range(6):
            self.assertIsNone(self.sys.access.verify("admin1", "wrong", "ADC-1"))
        self.assertEqual(self.sys.access.verify("admin1", demo_password("admin1"), "ADC-1"), "Administrator")

    def test_corrupt_frame_logged_and_refused(self):
        self.adc.command(["corrupt"])
        self.assertTrue(wait_for(lambda: any(e["type"] == "REJECTED" for e in self.sys.log.query({}))))

    def test_inventory_mismatch_raises_alarm(self):
        doc = self.login("doctor1")
        oid = self.sys.orders.submit(doc, "ADC-1", "Insulin", 5, True)
        self.adc.command(["deposit", oid])
        self.adc.command(["count"])
        self.assertTrue(wait_for(lambda: any(a["type"] == "COUNT_MISMATCH" for a in self.sys.alarms.active())))

    def test_speaker_failure_raises_alarm(self):
        self.host.speaker_broken = True
        self.adc.command(["alarm", "DOOR_FAULT", "critical"])
        self.assertTrue(wait_for(lambda: any(a["type"] == "SPEAKER_FAULT" for a in self.sys.alarms.active())))

    def test_ui_api(self):
        import json
        import urllib.request

        def post(path, body):
            req = urllib.request.Request("http://127.0.0.1:%d%s" % (self.sys.ui_port, path),
                                         json.dumps(body).encode(), {"Content-Type": "application/json"})
            return json.loads(urllib.request.urlopen(req).read())
        page = urllib.request.urlopen("http://127.0.0.1:%d/" % self.sys.ui_port).read()
        self.assertIn(b"Hospital Monitoring System", page)
        tok = post("/api/login", {"user": "doctor1", "password": demo_password("doctor1")})["token"]
        post("/api/order", {"token": tok, "adc": "ADC-1", "medication": "Insulin", "quantity": 2, "confirmed": True})
        st = post("/api/state", {"token": tok})
        self.assertEqual(st["adcs"][0]["state"], "Online")
        self.assertEqual(st["orders"][0]["status"], "Placed")


if __name__ == "__main__":
    unittest.main()
