"""1.2.4 Communication with HMS.

Secret: the network transport (TCP, one frame per line), the message format
(the HMS codec: JSON with CRC-32), sequence numbers, acknowledgements and the
offline queue. Implements FR-HMS-02, FR-HMS-07 (heartbeat every 0.5 s) and
NFR-HMS-01 (an ORDER_LIST is stored and acknowledged as soon as it arrives).

Every message to the HMS except HEARTBEAT and AUTH_REQUEST is kept until the
HMS acknowledges it. While the link is down, messages wait in an outbox kept in
non-volatile storage, and syncQueue sends them after the next HELLO. Each
message carries its original time in body["at"]; the frame gets a fresh
timestamp when it is actually sent, because the HMS refuses frames more than
60 s off its clock.

The HMS clock is the reference: every frame from the HMS carries its time, so
frames are stamped with our clock plus the last measured offset. A message the
HMS refused only for its timestamp is queued again and resent.
"""

import os
import socket
import sys
import threading
import time

try:
    from hms.protocol import (MessageCodec, Message, ChecksumError, FormatError,
                              UnsupportedRevision)
except ImportError:
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "..", "..", "..", "..", "monitor", "src"))
    from hms.protocol import (MessageCodec, Message, ChecksumError, FormatError,  # noqa: E402
                              UnsupportedRevision)

HEARTBEAT_PERIOD = 0.5
AUTH_TIMEOUT = 0.5
RECONNECT_DELAY = 1.0
MAX_RESENDS = 3
PROTOCOL = "1"
SOFTWARE = "adc-2.0"
FIRMWARE = "sim-hw-1.0"


class HmsComm:
    def __init__(self, adc_id, host, port, capacity, nvram, clock=time.time, log=None):
        self._id = adc_id
        self._host, self._port = host, port
        self._capacity = capacity
        self._nvram = nvram
        self._clock = clock
        self._say = log or (lambda text: print("[%s] %s" % (adc_id, text), flush=True))
        self._codec = MessageCodec()
        self._lock = threading.RLock()
        self._sock = None
        self._seq = 0
        self._offset = 0.0              # HMS clock minus ours
        self._outbox = list(nvram.read("outbox", []))
        self._unacked = {}
        self._auth_waits = {}
        self._auth_n = 0
        self._order_handler = None
        self._alive = False
        self._network = True
        self.users = []
        self.refused = []

    # ---- start and stop -------------------------------------------------------
    def start(self):
        self._alive = True
        threading.Thread(target=self._run, daemon=True).start()
        threading.Thread(target=self._beat, daemon=True).start()

    def stop(self):
        self._alive = False
        self._drop()

    def setNetworkAvailable(self, up):
        """Simulates unplugging the network cable."""
        self._network = up
        if not up:
            self._drop()

    def networkAvailable(self):
        return self._network

    # ---- exported operations ----------------------------------------------------
    def sendMessage(self, type_, body):
        """Returns "sent" or "queued"."""
        item = {"type": type_, "body": dict(body, at=self._clock())}
        with self._lock:
            if self._sock is not None and not self._outbox and self._transmit(item):
                return "sent"
            self._outbox.append(item)
            self._save()
            return "queued"

    def checkAuth(self, userID, password, timeout=AUTH_TIMEOUT):
        """("ok", role), ("denied", None) or ("unreachable", None) within the timeout."""
        waiter = {"event": threading.Event(), "result": None}
        with self._lock:
            if self._sock is None:
                return "unreachable", None
            self._auth_n += 1
            rid = self._auth_n
            self._auth_waits[rid] = waiter
            sent = self._send_raw("AUTH_REQUEST", {"request": rid, "user": userID, "password": password})
        if sent is None:
            self._auth_waits.pop(rid, None)
            return "unreachable", None
        answered = waiter["event"].wait(timeout)
        self._auth_waits.pop(rid, None)
        if not answered:
            return "unreachable", None
        r = waiter["result"]
        return ("ok", r.get("role", "")) if r.get("ok") else ("denied", None)

    def isConnected(self):
        return self._sock is not None

    def getQueuedCount(self):
        with self._lock:
            return len(self._outbox) + len(self._unacked)

    def syncQueue(self):
        """Send the outbox, oldest first. Returns how many were sent."""
        n = 0
        with self._lock:
            while self._outbox and self._sock is not None:
                if not self._transmit(self._outbox[0]):
                    break
                self._outbox.pop(0)
                n += 1
            if n:
                self._save()
        return n

    def registerNewOrderHandler(self, handler):
        """handler(list of {order, compartment, medication, quantity}); ACK is sent after it returns."""
        self._order_handler = handler

    # ---- connection ---------------------------------------------------------------
    def _run(self):
        while self._alive:
            if not self._network:
                time.sleep(0.2)
                continue
            try:
                s = socket.create_connection((self._host, self._port), timeout=3)
                s.settimeout(None)
            except OSError:
                time.sleep(RECONNECT_DELAY)
                continue
            with self._lock:
                if not self._alive or not self._network:
                    s.close()
                    continue
                self._sock, self._seq = s, 0
                self._send_raw("HELLO", {"capacity": self._capacity, "protocol": PROTOCOL,
                                         "software": SOFTWARE, "firmware": FIRMWARE})
                sent = self.syncQueue()
            self._say("connected to HMS%s" % (" (sent %d queued messages)" % sent if sent else ""))
            self._read(s)
            self._drop(s)
            self._say("disconnected from HMS")
            time.sleep(RECONNECT_DELAY)

    def _drop(self, s=None):
        with self._lock:
            if self._sock is None or (s is not None and self._sock is not s):
                return
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
            if self._unacked:
                self._outbox[:0] = [self._unacked[k] for k in sorted(self._unacked)]
                self._unacked = {}
                self._save()

    def _beat(self):
        while self._alive:
            self._send_raw("HEARTBEAT", {})
            time.sleep(HEARTBEAT_PERIOD)

    # ---- sending ---------------------------------------------------------------------
    def _send_raw(self, type_, body):
        """Send one frame now; returns its sequence number, or None if the link is down."""
        with self._lock:
            if self._sock is None:
                return None
            self._seq += 1
            data = self._codec.encode(Message(type_, self._id, self._seq, self._clock() + self._offset, body))
            try:
                self._sock.sendall(data + b"\n")
                return self._seq
            except OSError:
                self._drop()
                return None

    def _transmit(self, item):
        seq = self._send_raw(item["type"], item["body"])
        if seq is None:
            return False
        self._unacked[seq] = item
        return True

    def _save(self):
        pending = [self._unacked[k] for k in sorted(self._unacked)] + self._outbox
        self._nvram.write("outbox", pending)

    # ---- receiving ----------------------------------------------------------------------
    def _read(self, s):
        buf = b""
        try:
            while self._alive:
                chunk = s.recv(4096)
                if not chunk:
                    return
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if line.strip():
                        self._handle(line)
        except OSError:
            return

    def _handle(self, line):
        try:
            msg = self._codec.decode(line)
        except (ChecksumError, FormatError, UnsupportedRevision) as e:
            self._say("bad frame from HMS: %r" % e)
            return
        self._offset = msg.time - self._clock()
        b = msg.body
        if msg.type == "ACK":
            with self._lock:
                if self._unacked.pop(b.get("ack"), None) is not None:
                    self._save()
            return
        if msg.type == "NAK":
            reason = b.get("reason", "")
            with self._lock:
                item = self._unacked.pop(b.get("nak"), None)
                if item is None:
                    if not reason.startswith("timestamp"):
                        self._say("HMS refused a message: %s" % reason)
                    return
                if reason.startswith("timestamp") and item.get("resends", 0) < MAX_RESENDS:
                    item["resends"] = item.get("resends", 0) + 1
                    self._outbox.insert(0, item)
                    self._save()
                    self.syncQueue()
                    return
                self.refused.append({"message": item, "reason": reason})
                self._save()
            self._say("HMS refused a message: %s" % reason)
            return
        if msg.type == "AUTH_RESULT":
            waiter = self._auth_waits.get(b.get("request"))
            if waiter is not None:
                waiter["result"] = b
                waiter["event"].set()
        elif msg.type == "ORDER_LIST":
            if self._order_handler is not None:
                try:
                    self._order_handler(list(b.get("orders", [])))
                except Exception as e:
                    self._say("order list handler failed: %r" % e)
                    return
        elif msg.type == "ROLE_UPDATE":
            self.users = list(b.get("users", []))
        else:
            return
        self._send_raw("ACK", {"ack": msg.seq})
