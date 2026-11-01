"""Tests for the Deliverable 2 review fixes: the connection-state transition table,
the self-check of the log chain, the alarm on a full ADC, and the HELLO time check."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))                 # local layout: hms/ beside tests/
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
sys.path.insert(0, os.path.join(HERE, "..", "src"))          # repository layout: monitor/src
sys.path.insert(0, os.path.join(HERE, "..", "..", "adc", "src"))

from hms.health import health_monitor as HM   # noqa: E402
import test_hms as base                          # noqa: E402
from test_hms import wait_for                  # noqa: E402

STATES = ("Unknown", "Online", "Warning", "Offline")
EVENTS = (HM.NO_HEARTBEAT_YET, HM.HEARTBEAT_FRESH, HM.SILENT_1_5S, HM.SILENT_3S)


class TransitionTableTests(unittest.TestCase):
    def test_every_cell_is_decided(self):
        # Week 7: a blank cell is a transition nobody decided
        for s in STATES:
            self.assertEqual(set(HM.TRANSITIONS[s]), set(EVENTS), s)
            for e in EVENTS:
                self.assertIn(HM.TRANSITIONS[s][e], STATES)

    def test_event_table_is_disjoint_and_complete(self):
        # one event for each input case, at the boundaries too
        self.assertEqual(HM.event_of(None, 0.0, 2.99), HM.NO_HEARTBEAT_YET)
        self.assertEqual(HM.event_of(None, 0.0, 3.0), HM.SILENT_3S)
        self.assertEqual(HM.event_of(10.0, 0.0, 11.49), HM.HEARTBEAT_FRESH)
        self.assertEqual(HM.event_of(10.0, 0.0, 11.5), HM.SILENT_1_5S)
        self.assertEqual(HM.event_of(10.0, 0.0, 12.99), HM.SILENT_1_5S)
        self.assertEqual(HM.event_of(10.0, 0.0, 13.0), HM.SILENT_3S)

    def test_heartbeat_always_brings_an_adc_online(self):
        for s in STATES:
            self.assertEqual(HM.TRANSITIONS[s][HM.HEARTBEAT_FRESH], "Online")


class ReviewFixSystemTests(unittest.TestCase):
    # same running system as test_hms.SystemTests, without re-running its tests
    setUp = base.SystemTests.setUp
    tearDown = base.SystemTests.tearDown
    login = base.SystemTests.login

    def test_self_check_finds_tampered_log(self):
        self.sys.log._conn.execute("UPDATE log SET hash='00' WHERE id=1")
        self.host.advance(11.0)
        self.assertTrue(wait_for(lambda: any(a["type"] == "LOG_TAMPERED"
                                             for a in self.sys.alarms.active())))

    def test_full_adc_refuses_order_and_raises_alarm(self):
        doc = self.login("doctor1")
        nurse = self.login("nurse1")
        from hms.orders import AdcFullError
        for _ in range(8):              # the simulated ADC has 8 compartments
            self.sys.orders.submit(doc, "ADC-1", "Insulin", 1, True)
        for a in self.sys.alarms.active():      # staff acknowledge the 95% capacity alarm
            self.sys.alarms.acknowledge(nurse, a["id"])
        with self.assertRaises(AdcFullError):
            self.sys.orders.submit(doc, "ADC-1", "Insulin", 1, True)
        self.assertTrue(any(a["type"] == "ADC_FULL" for a in self.sys.alarms.active()))


    def test_report_through_the_web_page(self):
        import json
        import urllib.request
        from hms.access import demo_password

        def post(path, body):
            req = urllib.request.Request("http://127.0.0.1:%d%s" % (self.sys.ui_port, path),
                                         json.dumps(body).encode(), {"Content-Type": "application/json"})
            return json.loads(urllib.request.urlopen(req).read())
        page = urllib.request.urlopen("http://127.0.0.1:%d/" % self.sys.ui_port).read()
        self.assertIn(b'id="l-report"', page)
        tok = post("/api/login", {"user": "admin1", "password": demo_password("admin1")})["token"]
        j = post("/api/report", {"token": tok, "adc": "ADC-1"})
        self.assertIn("Entries by type:", j["text"])
        self.assertIn("ADC-1", j["text"])
        self.assertTrue(j["file"].endswith(".txt"))
        self.assertTrue(any(e["type"] == "REPORT" for e in self.sys.log.query({"type": "REPORT"})))


class HelloTimeTests(unittest.TestCase):
    def test_hello_with_a_wrong_clock_is_refused(self):
        from hms.gateway import adc_gateway as G
        src = open(G.__file__, encoding="utf-8").read()
        hello = src[src.index("def _on_hello"):src.index("def _accept")]
        self.assertIn("MAX_SKEW", hello)


if __name__ == "__main__":
    unittest.main()
