"""Cabinet panel: a local web page that stands in for the physical cabinet.

Secret: the page layout and the HTTP/JSON between the page and the cabinet.
The page is the touch screen and keypad (keyboard + display modules), the door
and tamper switch (door module), the buzzer light (alarm hardware), the mains
power and the network cable.
"""

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "panel.html")


class Panel:
    def __init__(self, cabinet):
        self.cabinet = cabinet
        self._server = None
        self.port = None

    def start(self, port, bind="127.0.0.1"):
        panel = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj, ctype="application/json"):
                body = obj if isinstance(obj, bytes) else json.dumps(obj).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/":
                    with open(PAGE, "rb") as f:
                        return self._send(200, f.read(), "text/html; charset=utf-8")
                if self.path == "/api/state":
                    return self._send(200, panel.cabinet.snapshot())
                self._send(404, {"error": "not found"})

            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                try:
                    q = json.loads(self.rfile.read(n) or b"{}")
                    if not isinstance(q, dict):
                        raise ValueError
                except ValueError:
                    return self._send(400, {"error": "bad request"})
                try:
                    result = panel.handle(self.path, q)
                except (KeyError, ValueError) as e:
                    return self._send(400, {"error": str(e)})
                except Exception as e:                  # never hide an unexpected failure
                    return self._send(500, {"error": "internal error: %r" % (e,)})
                self._send(200, {"result": result, "state": panel.cabinet.snapshot()})

        self._server = ThreadingHTTPServer((bind, port), Handler)
        self.port = self._server.server_address[1]
        Thread(target=self._server.serve_forever, daemon=True).start()

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()

    def handle(self, path, q):
        cab = self.cabinet
        if path == "/api/input":
            data = {k: v for k, v in q.items() if k != "kind"}
            return cab.press(str(q.get("kind", "")), **data)
        if path == "/api/door":
            return cab.set_door_open(bool(q.get("open")))
        if path == "/api/tamper":
            cab.set_tamper(bool(q.get("on")))
            return {"ok": True}
        if path == "/api/power":
            if q.get("on"):
                cab.power_on()
            else:
                cab.power_off()
            return {"ok": True}
        if path == "/api/network":
            cab.set_network(bool(q.get("up")))
            return {"ok": True}
        raise KeyError("unknown path " + path)
