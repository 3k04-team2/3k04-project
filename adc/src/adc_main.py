"""Automated Dispensing Cabinet (Deliverable 2): starts the cabinet and its web panel.

Run (from the repo root, with the HMS already running):
    py adc/src/adc_main.py                         ADC-1, HMS on 127.0.0.1:9000, panel on port 8090
    py adc/src/adc_main.py --id ADC-2 --ui-port 8091
Open http://127.0.0.1:8090 (see adc/src/README.md).

This file is not called main.py because the ADC tests put both adc/src and
monitor/src on the import path and import the monitor's main.py.
"""

import argparse
import os
import time

from adc.cabinet import Cabinet
from adc.ui import Panel

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    ap = argparse.ArgumentParser(description="Automated Dispensing Cabinet")
    ap.add_argument("--id", default="ADC-1", help="name of this cabinet")
    ap.add_argument("--hms-host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9000, help="HMS port for cabinets")
    ap.add_argument("--capacity", type=int, default=16, help="number of compartments")
    ap.add_argument("--ui-port", type=int, default=8090, help="port of the cabinet panel")
    ap.add_argument("--data", default=None, help="folder for the cabinet's memory (default adc/src/data/<id>)")
    ap.add_argument("--lan", action="store_true", help="serve the panel on all network interfaces")
    a = ap.parse_args()
    data = a.data or os.path.join(HERE, "data", a.id)
    cabinet = Cabinet(a.id, a.hms_host, a.port, a.capacity, data)
    cabinet.power_on()
    panel = Panel(cabinet)
    panel.start(a.ui_port, "0.0.0.0" if a.lan else "127.0.0.1")
    print("%s running (%d compartments). HMS %s:%d. Panel: http://127.0.0.1:%d"
          % (a.id, a.capacity, a.hms_host, a.port, panel.port), flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        panel.stop()
        cabinet.stop()


if __name__ == "__main__":
    main()
