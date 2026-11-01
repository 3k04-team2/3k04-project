# Hospital Monitoring System (monitor)

Python 3.10 or newer, standard library only. On Windows the command is `py`.

## Run the demo

1. From the repo root, start the monitor: `py monitor/src/main.py --sim-host`
   (`--sim-host` adds a simulated clock, speaker and disk, with demo buttons on the page. Leave it off to use the real clock and speaker.)
2. In a second window start two simulated cabinets: `py adc/src/adc_sim.py --count 2`
3. Open http://127.0.0.1:8080

Options: `--port` (cabinet port, default 9000), `--ui-port` (default 8080), `--data` (folder for saved state and log), `--lan`.

Demo accounts are doctor1, nurse1, pharmacist1, tech1, admin1 and it1 (roles and passwords are defined in `monitor/src/hms/access/access_control.py`). Five wrong passwords from one device lock it for one hour. A session ends after 600 s without activity.

## Where each module is (`monitor/src/hms/`)

M1 `link`, M2 `protocol`, M3 `gateway`, M4 `health`, M5 `orders`, M6 `inventory`, M7 `alarms`, M8 `access`, M9 `store/log_store.py`, M10 `store/state_store.py`, M11 `ui`, M12 `host`. `monitor/src/main.py` connects them. The cabinet simulator is `adc/src/adc_sim.py` (type `status` or see its header for commands).

## Tests

`py -m unittest discover -s monitor/tests` (21 tests, about 12 s)
