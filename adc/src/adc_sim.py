"""ADC simulator: pretends to be one or more Automated Dispensing Cabinets.

Run:  py adc/src/adc_sim.py --count 2
Type commands into this window (optionally start with an ADC name, e.g. "ADC-2 stop"):
  stop / resume      stop or restart the heartbeats (the HMS marks the ADC Warning, then Offline)
  corrupt            send one frame with a wrong checksum (the HMS refuses it and logs it)
  alarm [TYPE] [critical|warning]   raise an alarm from the ADC
  clear [TYPE]       clear that alarm
  deposit ORD-1      the pharmacist stocks that order into its compartment
  withdraw ORD-1     the doctor takes it out (order becomes Fulfilled)
  count              report a wrong inventory once (the HMS raises a mismatch alarm)
  noack / ack        stop or start acknowledging the HMS's messages
  drop               close the connection (the ADC reconnects after one second)
  status             print what this ADC holds
  quit
"""

import argparse
import os
import socket
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "monitor", "src"))   # for the message codec
from hms.protocol import MessageCodec, Message  # noqa: E402

CODEC = MessageCodec()


class Adc:
    def __init__(self, adc_id, host, port, capacity):
        self.id, self.host, self.port, self.capacity = adc_id, host, port, capacity
        self.sock = None
        self.seq = 0
        self.lock = threading.Lock()
        self.heartbeat = True
        self.acking = True
        self.wrong_count = False
        self.orders = {}                  # order id -> {compartment, medication, quantity}
        self.contents = {}                # compartment -> {medication, quantity}
        self.alive = True

    def say(self, text):
        print("[%s] %s" % (self.id, text), flush=True)

    # ---- sending -----------------------------------------------------------------
    def send(self, type_, body, corrupt=False):
        with self.lock:
            if self.sock is None:
                return False
            self.seq += 1
            data = CODEC.encode(Message(type_, self.id, self.seq, time.time(), body))
            if corrupt:
                data = data.replace(b'"crc":', b'"crc":1')
            try:
                self.sock.sendall(data + b"\n")
                return True
            except OSError:
                return False

    def send_inventory(self):
        comps = [{"compartment": k, "medication": c["medication"], "quantity": c["quantity"]}
                 for k, c in sorted(self.contents.items())]
        if self.wrong_count and comps:
            comps[0]["quantity"] += 1
            self.wrong_count = False
        self.send("INVENTORY", {"compartments": comps})

    # ---- connection loop -------------------------------------------------------------
    def run(self):
        threading.Thread(target=self._beat, daemon=True).start()
        while self.alive:
            try:
                s = socket.create_connection((self.host, self.port), timeout=3)
                s.settimeout(None)
            except OSError:
                time.sleep(1)
                continue
            with self.lock:
                self.sock, self.seq = s, 0
            self.send("HELLO", {"capacity": self.capacity, "protocol": "1",
                                "software": "sim-1.0", "firmware": "sim-1.0"})
            self.say("connected")
            self._read(s)
            with self.lock:
                self.sock = None
            self.say("disconnected")
            time.sleep(1)

    def _read(self, s):
        buf = b""
        try:
            while self.alive:
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
            msg = CODEC.decode(line)
        except Exception as e:
            self.say("bad frame from HMS: %r" % e)
            return
        if msg.type == "ORDER_LIST":
            self.orders = {o["order"]: o for o in msg.body.get("orders", [])}
            self.say("order list: %s" % (", ".join("%s(%s x%s in %s)" % (k, o["medication"], o["quantity"], o["compartment"])
                                                  for k, o in self.orders.items()) or "empty"))
        elif msg.type == "ROLE_UPDATE":
            self.say("role update: %d users" % len(msg.body.get("users", [])))
        elif msg.type == "ACK":
            return
        elif msg.type == "NAK":
            self.say("HMS refused a message: %s" % msg.body.get("reason"))
            return
        else:
            return
        if self.acking:
            self.send("ACK", {"ack": msg.seq})
        else:
            self.say("(not acknowledging)")

    def _beat(self):
        while self.alive:
            if self.heartbeat:
                self.send("HEARTBEAT", {})
            time.sleep(0.5)

    # ---- commands -----------------------------------------------------------------------
    def command(self, words):
        cmd, args = words[0], words[1:]
        if cmd == "stop":
            self.heartbeat = False
        elif cmd == "resume":
            self.heartbeat = True
        elif cmd == "corrupt":
            self.send("HEARTBEAT", {}, corrupt=True)
        elif cmd == "alarm":
            self.send("ALARM", {"alarm": args[0] if args else "DOOR_FAULT",
                                "severity": args[1] if len(args) > 1 else "critical",
                                "text": "simulated fault"})
        elif cmd == "clear":
            self.send("ALARM_CLEARED", {"alarm": args[0] if args else "DOOR_FAULT"})
        elif cmd in ("deposit", "withdraw"):
            self._move(cmd, args[0] if args else "")
        elif cmd == "count":
            self.wrong_count = True
            self.send_inventory()
        elif cmd == "noack":
            self.acking = False
        elif cmd == "ack":
            self.acking = True
        elif cmd == "drop":
            with self.lock:
                if self.sock:
                    self.sock.close()
        elif cmd == "status":
            self.say("heartbeat=%s acking=%s orders=%s contents=%s" %
                     (self.heartbeat, self.acking, sorted(self.orders), self.contents))
        else:
            self.say("unknown command " + cmd)

    def _move(self, kind, order_id):
        o = self.orders.get(order_id)
        if o is None:
            self.say("no such order here: %s" % order_id)
            return
        k, med, qty = o["compartment"], o["medication"], o["quantity"]
        user = "pharmacist1" if kind == "deposit" else "doctor1"
        self.send("EVENT", {"kind": kind, "medication": med, "quantity": qty, "compartment": k,
                            "user": user, "order": order_id})
        if kind == "deposit":
            self.contents[k] = {"medication": med, "quantity": qty}
        else:
            self.contents.pop(k, None)
        self.send("ORDER_STATUS", {"orders": [{"order": order_id,
                                               "status": "Stocked" if kind == "deposit" else "Fulfilled"}]})
        self.send_inventory()


def main():
    ap = argparse.ArgumentParser(description="ADC simulator")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9000)
    ap.add_argument("--count", type=int, default=1)
    ap.add_argument("--capacity", type=int, default=16)
    ap.add_argument("--first", type=int, default=1, help="number of the first ADC")
    a = ap.parse_args()
    adcs = [Adc("ADC-%d" % (a.first + i), a.host, a.port, a.capacity) for i in range(a.count)]
    for adc in adcs:
        threading.Thread(target=adc.run, daemon=True).start()
    print(__doc__)
    for line in sys.stdin:
        words = line.split()
        if not words:
            continue
        if words[0] == "quit":
            break
        targets = adcs
        if words[0].upper() in [x.id for x in adcs]:
            targets = [x for x in adcs if x.id == words[0].upper()]
            words = words[1:]
            if not words:
                continue
        for adc in targets:
            adc.command(words)
    for adc in adcs:
        adc.alive = False


if __name__ == "__main__":
    main()
