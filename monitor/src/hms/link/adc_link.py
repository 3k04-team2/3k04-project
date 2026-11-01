"""M1 ADC Link.

Secret: the network transport (TCP sockets now; could become TLS, serial or a
message broker). Moves framed bytes to and from each ADC; one frame is one line.
Connections are named by an id the link gives them. Uses: no other module.
"""

import socket
import threading

MAX_FRAME = 65536


class PortInUse(Exception):
    pass


class NotConnected(Exception):
    pass


class AdcLink:
    def __init__(self):
        self._srv = None
        self._conns = {}
        self._n = 0
        self._lock = threading.Lock()
        self._on_receive = None
        self._on_disconnect = None
        self._stopped = False
        self.port = None

    def onReceive(self, handler):
        self._on_receive = handler

    def onDisconnect(self, handler):
        self._on_disconnect = handler

    def start(self, port, bind="0.0.0.0"):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind((bind, port))
        except OSError:
            s.close()
            raise PortInUse(port)
        s.listen(16)
        self._srv = s
        self.port = s.getsockname()[1]
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def stop(self):
        self._stopped = True
        if self._srv:
            try:
                self._srv.close()
            except OSError:
                pass
        with self._lock:
            conns = list(self._conns.values())
        for c in conns:
            try:
                c[0].close()
            except OSError:
                pass

    def started(self):
        return self._srv is not None and not self._stopped

    def send(self, conn_id, data):
        with self._lock:
            entry = self._conns.get(conn_id)
        if entry is None:
            raise NotConnected(conn_id)
        sock, send_lock = entry
        try:
            with send_lock:
                sock.sendall(data + b"\n")
        except OSError:
            self._drop(conn_id)
            raise NotConnected(conn_id)

    def _accept_loop(self):
        while not self._stopped:
            try:
                sock, _ = self._srv.accept()
            except OSError:
                return
            with self._lock:
                self._n += 1
                conn_id = "conn-%d" % self._n
                self._conns[conn_id] = (sock, threading.Lock())
            threading.Thread(target=self._reader, args=(conn_id, sock), daemon=True).start()

    def _reader(self, conn_id, sock):
        buf = b""
        try:
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if line.strip() and self._on_receive:
                        try:
                            self._on_receive(conn_id, line)
                        except Exception as e:  # a handler bug must not kill the connection
                            print("AdcLink: handler error:", repr(e))
                if len(buf) > MAX_FRAME:
                    break
        except OSError:
            pass
        self._drop(conn_id)

    def _drop(self, conn_id):
        with self._lock:
            entry = self._conns.pop(conn_id, None)
        if entry is None:
            return
        try:
            entry[0].close()
        except OSError:
            pass
        if self._on_disconnect:
            try:
                self._on_disconnect(conn_id)
            except Exception as e:
                print("AdcLink: disconnect handler error:", repr(e))
