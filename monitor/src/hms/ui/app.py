"""M11 User Interface.

Secret: the UI toolkit, screen layout, colours, sounds and report format (a
local web page served over HTTP, JSON between the page and the server).
Implements the screens and the confirmation before every protected action.
Uses: M4, M5, M6, M7, M8, M9. The page file is read through the Host Platform,
which is passed in by main.py.
"""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from hms.errors import NotAllowed, SessionExpired
from hms.access import BadCredentials, DeviceLocked
from hms.orders import OrderError
from hms.store import NoMatches, RetentionViolation

PAGE = os.path.join(os.path.dirname(__file__), "index.html")


class WebApp:
    def __init__(self, host, health, orders, inventory, alarms, access, log, sim=None):
        self._host = host
        self.health, self.orders, self.inventory = health, orders, inventory
        self.alarms, self.access, self.log = alarms, access, log
        self.sim = sim            # the simulated host, when the demo uses one
        self._sessions = {}
        self._server = None
        self.port = None

    def start(self, port, bind="127.0.0.1"):
        app = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj, ctype="application/json"):
                body = obj if isinstance(obj, bytes) else json.dumps(obj).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                url = urlparse(self.path)
                if url.path == "/":
                    return self._send(200, app._host.read_file(PAGE), "text/html; charset=utf-8")
                q = {k: v[0] for k, v in parse_qs(url.query).items()}
                self._dispatch("GET", url.path, q)

            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                try:
                    q = json.loads(self.rfile.read(n) or b"{}")
                except ValueError:
                    return self._send(400, {"error": "bad request"})
                self._dispatch("POST", urlparse(self.path).path, q)

            def _dispatch(self, method, path, q):
                try:
                    out = app.handle(method, path, q, self.client_address[0])
                    self._send(200, out)
                except (NotAllowed,) as e:
                    self._send(403, {"error": "not allowed: %s" % e})
                except SessionExpired:
                    self._send(401, {"error": "session expired, log in again"})
                except (BadCredentials,):
                    self._send(401, {"error": "wrong user name or password"})
                except DeviceLocked:
                    self._send(423, {"error": "this device is locked for one hour"})
                except OrderError as e:
                    self._send(409, {"error": e.reason, "suggestions": e.suggestions})
                except (NoMatches, RetentionViolation, KeyError, ValueError) as e:
                    self._send(400, {"error": "%s" % (e,)})
                except Exception as e:                      # never hide an unexpected failure
                    print("UI error:", repr(e))
                    self._send(500, {"error": "unexpected error: %r" % (e,)})

        self._server = ThreadingHTTPServer((bind, port), Handler)
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def stop(self):
        if self._server:
            self._server.shutdown()

    # ---- the operations the page can call ------------------------------------
    def _session(self, q):
        s = self._sessions.get(q.get("token"))
        if s is None:
            raise SessionExpired("no session")
        self.access.check(s)                  # the page's own polling is not user activity
        return s

    def handle(self, method, path, q, client):
        if path == "/api/login":
            s = self.access.login(q.get("user", ""), q.get("password", ""), client)
            self._sessions[s.id] = s
            return {"token": s.id, "user": s.user, "role": s.role}
        s = self._session(q)
        if path == "/api/logout":
            self.access.logout(s)
            self._sessions.pop(s.id, None)
            return {}
        if path == "/api/state":
            return self._state(s)
        if path == "/api/order":
            oid = self.orders.submit(s, q["adc"], q["medication"], int(q["quantity"]), bool(q.get("confirmed")))
            return {"order": oid}
        if path == "/api/cancel":
            self.orders.cancel(s, q["order"])
            return {}
        if path == "/api/fulfil":
            self.orders.markFulfilled(s, q["order"])
            return {}
        if path == "/api/ack":
            self.alarms.acknowledge(s, q["alarm"])
            return {}
        if path == "/api/clear_maintenance":
            self.health.clearMaintenance(s, q["adc"])
            return {}
        if path == "/api/logs":
            self._need(s, "view_logs")
            flt = {k: q[k] for k in ("adc", "user", "type") if q.get(k)}
            return {"entries": self.log.query(flt, "desc")[:300], "tampered": self.log.verify()}
        if path == "/api/export":
            self._need(s, "view_logs")
            flt = {k: q[k] for k in ("adc", "user", "type") if q.get(k)}
            return {"file": self.log.exportCsv(flt, s.user)}
        if path == "/api/report":
            self._need(s, "view_logs")
            flt = {k: q[k] for k in ("adc", "user", "type") if q.get(k)}
            file, text = self.log.report(flt, s.user)
            return {"file": file, "text": text}
        if path == "/api/roles":
            return {"users": self.access.users(s),
                    "roles": ["Doctor", "Nurse", "Pharmacist", "Technician", "Administrator", "IT"]}
        if path == "/api/set_role":
            return {"sync": self.access.setRole(s, q["user"], q["role"])}
        if path == "/api/sim" and self.sim is not None:
            if q.get("advance"):
                self.sim.advance(float(q["advance"]))
            if "speaker_broken" in q:
                self.sim.speaker_broken = bool(q["speaker_broken"])
            if "disk_full" in q:
                self.sim.disk_full = bool(q["disk_full"])
            return {}
        raise KeyError("unknown request " + path)

    def _need(self, session, action):
        if not self.access.allowed(session, action):
            raise NotAllowed(action)

    def _state(self, s):
        status = self.health.systemStatus()
        adcs = []
        for adc_id, info in sorted(status["adcs"].items()):
            c = self.inventory.contents(adc_id)
            adcs.append({"id": adc_id, "state": info["state"], "maintenance": info["maintenance"],
                         "versions": info["versions"], "stale": c["stale"], "updated": c["updated"],
                         "capacity": c["capacity"], "free": c["free"], "occupancy": c["occupancy"],
                         "compartments": c["compartments"]})
        perms = self.access.role_perms(s.role)
        out = {"user": s.user, "role": s.role, "perms": perms, "mode": status["mode"],
               "components": status["components"], "adcs": adcs,
               "alarms": self.alarms.recent(), "tone": self.alarms.toneOn(),
               "orders": self.orders.orders(), "formulary": self.orders.formulary(),
               "notices": self.alarms.notices_for(s.user, s.role), "now": self._host.now(),
               "sim": None}
        if self.sim is not None:
            out["sim"] = {"speaker_broken": self.sim.speaker_broken, "disk_full": self.sim.disk_full}
        return out
