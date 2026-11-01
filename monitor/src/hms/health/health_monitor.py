"""M4 Health Monitor.

Secret: the rules for judging health: the heartbeat thresholds and the
self-check list and period. Implements SPEC-01, the maintenance flag of SPEC-04
and the self-check, backups and recovery of SPEC-11.
Uses: M3 ADC Gateway, M7 Alarm Manager, M9 Log Store, M10 State Store.
(The authorize function that checks the technician's permission is passed in by
main.py; it is M8's allowed operation.)
"""

import threading

from hms.errors import NotAllowed
from hms.host import DiskError

WARNING_AFTER = 1.5
OFFLINE_AFTER = 3.0
SELF_CHECK_EVERY = 10.0
BACKUP_EVERY = 24 * 3600.0


class _Adc:
    def __init__(self, t_reg):
        self.t_reg = t_reg
        self.t_hb = None
        self.state = "Unknown"
        self.maint = False


class HealthMonitor:
    def __init__(self, gateway, alarms, log, state, host, authorize=None):
        self._gateway = gateway
        self._alarms = alarms
        self._log = log
        self._state = state
        self._host = host
        self._authorize = authorize
        self._lock = threading.RLock()
        self._adcs = {}
        self._handlers = []
        self._mode = "Normal"
        self._components = {}
        self._failed = set()
        self._last_check = host.monotonic()
        self._last_backup = host.monotonic()
        # after a restart every known ADC starts as Unknown (SPEC-01, SPEC-11)
        now = host.monotonic()
        for adc_id in state.load("adcs", []):
            self._adcs[adc_id] = _Adc(now)
        for adc_id in state.load("maintenance", []):
            if adc_id in self._adcs:
                self._adcs[adc_id].maint = True

    # ---- handlers registered with M3 -------------------------------------
    def onHello(self, adcId, msg):
        with self._lock:
            if adcId not in self._adcs:
                self._adcs[adcId] = _Adc(self._host.monotonic())
                self._save_adcs()

    def onHeartbeat(self, adcId, msg):
        with self._lock:
            st = self._adcs.get(adcId)
            if st is not None:
                st.t_hb = self._host.monotonic()

    def onAlarm(self, adcId, msg):
        """A fault or alarm from the ADC also flags it for maintenance (SPEC-04)."""
        self._flag(adcId, "alarm from the ADC: %s" % msg.body.get("alarm", ""))

    # ---- SPEC-01 and SPEC-11 -------------------------------------------------
    def tick(self, now):
        with self._lock:
            changes = []
            for adc_id, st in self._adcs.items():
                new = self._compute(st, now)
                if new != st.state:
                    changes.append((adc_id, st.state, new))
                    st.state = new
        for adc_id, old, new in changes:
            self._log.append({"adc": adc_id, "type": "STATE", "data": "%s -> %s" % (old, new)})
            if new == "Offline":
                self._flag(adc_id, "ADC is Offline")
            for h in list(self._handlers):
                h(adc_id, old, new)
        if now - self._last_check >= SELF_CHECK_EVERY:
            self._last_check = now
            self._self_check()
        if now - self._last_backup >= BACKUP_EVERY:
            self._last_backup = now
            self._backup()

    @staticmethod
    def _compute(st, now):
        if st.t_hb is None:
            return "Unknown" if now - st.t_reg < OFFLINE_AFTER else "Offline"
        delta = now - st.t_hb
        if delta < WARNING_AFTER:
            return "Online"
        return "Warning" if delta < OFFLINE_AFTER else "Offline"

    def _self_check(self):
        results = {"gateway": self._gateway.health(), "alarms": self._alarms.health(),
                   "log": self._log.health(), "state": self._state.health()}
        self._components = results
        for name, ok in results.items():
            if not ok and name not in self._failed:
                self._failed.add(name)
                self._alarms.raise_("SELF_CHECK_FAILED", "critical", "", "%s failed its health check" % name)
            elif ok and name in self._failed:
                self._failed.discard(name)
                self._log.append({"type": "SELF_CHECK_OK", "data": name + " recovered"})
        if self._failed:
            self._mode = "Fault"
        elif any(st.maint for st in self._adcs.values()):
            self._mode = "Maintenance"
        else:
            self._mode = "Normal"

    def _backup(self):
        try:
            self._log.backup()
            self._state.backup()
            self._log.append({"type": "BACKUP", "data": "log and state backed up"})
        except DiskError as e:
            self._alarms.raise_("BACKUP_FAILED", "critical", "", "backup failed: %s" % e)

    # ---- maintenance flag (SPEC-04) --------------------------------------------
    def _flag(self, adcId, why):
        with self._lock:
            st = self._adcs.get(adcId)
            if st is None:
                return
            newly = not st.maint
            st.maint = True
            self._save_maintenance()
        if newly:
            self._alarms.raise_("MAINTENANCE_NEEDED", "warning", adcId, why)
            self._alarms.notify("Technician", "ADC %s needs maintenance (%s)" % (adcId, why))

    def maintenance(self, adcId):
        st = self._adcs.get(adcId)
        return bool(st and st.maint)

    def clearMaintenance(self, session, adcId):
        if self._authorize is None or not self._authorize(session, "clear_maintenance"):
            raise NotAllowed("clear_maintenance")
        with self._lock:
            st = self._adcs.get(adcId)
            if st is not None:
                st.maint = False
                self._save_maintenance()
        self._log.append({"adc": adcId, "user": session.user, "type": "MAINT_CLEARED", "data": ""})

    # ---- queries ----------------------------------------------------------------------
    def state(self, adcId):
        st = self._adcs.get(adcId)
        return st.state if st else "Unknown"

    def onStateChange(self, handler):
        self._handlers.append(handler)

    def systemStatus(self):
        with self._lock:
            adcs = {a: {"state": st.state, "maintenance": st.maint,
                        "versions": self._gateway.versions(a)} for a, st in self._adcs.items()}
        return {"mode": self._mode, "components": dict(self._components), "adcs": adcs}

    def _save_adcs(self):
        try:
            self._state.save("adcs", sorted(self._adcs))
        except DiskError:
            pass

    def _save_maintenance(self):
        try:
            self._state.save("maintenance", sorted(a for a, st in self._adcs.items() if st.maint))
        except DiskError:
            pass
