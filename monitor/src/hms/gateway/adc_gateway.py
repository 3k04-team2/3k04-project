"""M3 ADC Gateway.

Secret: per-ADC sessions, sequence numbers, ACK/NAK and retry rules, the bounds
checks, and which module receives which message type. Implements SPEC-02 for
every ADC at once. Uses: M1, M2, M9, M12.
"""

import threading
import time as _time

from hms.protocol import (Message, ChecksumError, FormatError, UnsupportedRevision)
from hms.link import NotConnected

TYPES = {"HELLO", "HEARTBEAT", "ORDER_STATUS", "EVENT", "INVENTORY",
         "ALARM", "ALARM_CLEARED", "LOG_BATCH", "AUTH_REQUEST", "ACK", "NAK"}
MAX_TEXT = 256
MAX_QUANTITY = 1000
MAX_SKEW = 60.0


class BoundsError(Exception):
    pass


class AdcUnavailable(Exception):
    pass


class NotAcknowledged(Exception):
    pass


class _Adc:
    def __init__(self, capacity):
        self.capacity = capacity
        self.conn = None
        self.last_seq = 0
        self.out_seq = 0
        self.acks = {}
        self.versions = {}


class AdcGateway:
    def __init__(self, link, codec, log, host, ack_timeout=10.0, retry=2.0):
        self._link = link
        self._codec = codec
        self._log = log
        self._host = host
        self._ack_timeout = ack_timeout
        self._retry = retry
        self._adcs = {}
        self._handlers = {}
        self._lock = threading.RLock()
        link.onReceive(self._on_frame)
        link.onDisconnect(self._on_disconnect)

    # ---- exported operations ------------------------------------------
    def register(self, adcId, capacity):
        with self._lock:
            st = self._adcs.get(adcId)
            if st is None:
                self._adcs[adcId] = _Adc(capacity)
            else:
                st.capacity = capacity

    def subscribe(self, type_, handler):
        self._handlers.setdefault(type_, []).append(handler)

    def send(self, adcId, msg):
        """Check bounds, send, retry every 2 s, return the acknowledged sequence number."""
        st = self._adcs.get(adcId)
        if st is None:
            raise AdcUnavailable(adcId)
        self._check_bounds(st, msg.body)
        with self._lock:
            st.out_seq += 1
            seq = st.out_seq
        msg.adc, msg.seq, msg.time = adcId, seq, self._host.now()
        data = self._codec.encode(msg)
        deadline = self._host.monotonic() + self._ack_timeout
        next_try = 0.0
        while True:
            verdict = st.acks.pop(seq, None)
            if verdict == "ACK":
                return seq
            if verdict == "NAK":
                raise NotAcknowledged("ADC refused sequence %d" % seq)
            now = self._host.monotonic()
            if now >= deadline:
                raise NotAcknowledged("no acknowledgement in %.0f s" % self._ack_timeout)
            if now >= next_try:
                conn = st.conn
                if conn is None:
                    raise AdcUnavailable(adcId)
                try:
                    self._link.send(conn, data)
                except NotConnected:
                    st.conn = None
                    raise AdcUnavailable(adcId)
                next_try = now + self._retry
            _time.sleep(0.01)

    def broadcast(self, msg):
        """Send to every registered ADC; returns {adcId: 'synced' or 'not confirmed'}."""
        result = {}
        threads = []

        def one(adc_id):
            copy = Message(msg.type, body=msg.body)
            try:
                self.send(adc_id, copy)
                result[adc_id] = "synced"
            except (AdcUnavailable, NotAcknowledged, BoundsError):
                result[adc_id] = "not confirmed"

        for adc_id in list(self._adcs):
            t = threading.Thread(target=one, args=(adc_id,), daemon=True)
            t.start()
            threads.append(t)
        for t in threads:
            t.join()
        return result

    def versions(self, adcId):
        st = self._adcs.get(adcId)
        return dict(st.versions) if st else {}

    def health(self):
        return self._link.started()

    # ---- receive path (SPEC-02) -----------------------------------------
    def _on_frame(self, conn, data):
        try:
            msg = self._codec.decode(data)
        except (ChecksumError, FormatError, UnsupportedRevision) as e:
            self._reject(conn, "", 0, "%s: %s" % (type(e).__name__, e))
            return
        if msg.type not in TYPES:
            self._reject(conn, msg.adc, msg.seq, "unknown type " + msg.type)
            return
        if msg.type == "HELLO":
            self._on_hello(conn, msg)
            return
        st = self._adcs.get(msg.adc)
        if st is None:
            self._reject(conn, msg.adc, msg.seq, "ADC is not registered")
            return
        if msg.seq <= st.last_seq:
            self._reject(conn, msg.adc, msg.seq, "sequence number not new")
            return
        try:
            self._check_bounds(st, msg.body)
            skew = abs(msg.time - self._host.now())
            if skew > MAX_SKEW:
                raise BoundsError("timestamp %.0f s away from HMS time" % skew)
        except BoundsError as e:
            self._reject(conn, msg.adc, msg.seq, str(e))
            return
        st.last_seq = msg.seq
        st.conn = conn
        self._accept(conn, st, msg)

    def _on_hello(self, conn, msg):
        cap = msg.body.get("capacity")
        if not isinstance(cap, int) or isinstance(cap, bool) or not 1 <= cap <= 1000:
            self._reject(conn, msg.adc, msg.seq, "bad capacity in HELLO")
            return
        try:
            self._check_bounds(None, msg.body)
        except BoundsError as e:
            self._reject(conn, msg.adc, msg.seq, str(e))
            return
        with self._lock:
            st = self._adcs.get(msg.adc)
            if st is None:
                st = self._adcs[msg.adc] = _Adc(cap)
            st.capacity = cap
            st.conn = conn
            st.last_seq = msg.seq          # a new session starts the ADC's numbering again
            st.versions = {"protocol": msg.body.get("protocol", ""),
                           "software": msg.body.get("software", ""),
                           "firmware": msg.body.get("firmware", "")}
        self._accept(conn, st, msg)

    def _accept(self, conn, st, msg):
        if msg.type == "ACK":
            st.acks[msg.body.get("ack")] = "ACK"
            return
        if msg.type == "NAK":
            st.acks[msg.body.get("nak")] = "NAK"
            return
        if msg.type != "HEARTBEAT":
            self._reply(conn, "ACK", msg.adc, {"ack": msg.seq})
        if msg.type == "LOG_BATCH":
            for e in msg.body.get("entries", []):
                if isinstance(e, dict):
                    self._log.append({"adc": msg.adc, "user": e.get("user", ""),
                                      "type": "ADC_" + str(e.get("type", "LOG")),
                                      "data": e.get("data", ""), "time": e.get("time", msg.time)})
        for handler in list(self._handlers.get(msg.type, [])):
            try:
                handler(msg.adc, msg)
            except Exception as e:
                self._log.append({"adc": msg.adc, "type": "HANDLER_ERROR",
                                  "data": "%s: %r" % (msg.type, e)})

    def _reject(self, conn, adc, seq, reason):
        self._log.append({"adc": adc, "type": "REJECTED", "data": reason})
        self._reply(conn, "NAK", adc or "HMS", {"nak": seq, "reason": reason})

    def _reply(self, conn, type_, adc, body):
        msg = Message(type_, adc=adc, seq=0, time=self._host.now(), body=body)
        try:
            self._link.send(conn, self._codec.encode(msg))
        except NotConnected:
            pass

    def _on_disconnect(self, conn):
        with self._lock:
            for st in self._adcs.values():
                if st.conn == conn:
                    st.conn = None

    def _check_bounds(self, st, body):
        cap = st.capacity if st else 1000

        def walk(x, key=""):
            if isinstance(x, dict):
                for k, v in x.items():
                    walk(v, k)
            elif isinstance(x, list):
                for v in x:
                    walk(v, key)
            elif isinstance(x, str):
                if len(x) > MAX_TEXT:
                    raise BoundsError("text longer than %d characters" % MAX_TEXT)
            elif isinstance(x, (int, float)) and not isinstance(x, bool):
                if key == "compartment" and not (isinstance(x, int) and 0 <= x <= cap - 1):
                    raise BoundsError("compartment %s outside 0..%d" % (x, cap - 1))
                if key == "quantity" and not (isinstance(x, int) and 0 <= x <= MAX_QUANTITY):
                    raise BoundsError("quantity %s outside 0..%d" % (x, MAX_QUANTITY))

        walk(body)
