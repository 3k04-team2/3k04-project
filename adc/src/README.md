# Automated Dispensing Cabinet (adc)

Python 3.10 or newer, standard library only. On Windows the command is `py`.
This is the Deliverable 2 design implemented: the 4-variable model, the REQ tables and the module interfaces.

## Run the demo

1. From the repo root, start the HMS: `py monitor/src/main.py --sim-host`
2. In a second window start a cabinet: `py adc/src/adc_main.py`
   (a second cabinet: `py adc/src/adc_main.py --id ADC-2 --ui-port 8091`)
3. Open the cabinet panel at http://127.0.0.1:8090 and the HMS at http://127.0.0.1:8080

Options: `--id` (default ADC-1), `--hms-host`, `--port` (HMS cabinet port, default 9000), `--capacity` (default 16),
`--ui-port` (default 8090), `--data` (folder for the cabinet's memory, default `adc/src/data/<id>`), `--lan`.

The panel stands in for the physical cabinet. It has the screen, the keypad and badge login, the barcode scan, and the compartment sensor.
It also lets you open and close the door, trigger the tamper switch, cut the mains power and unplug the network.
Logins are checked by the HMS, so use its demo accounts. The password is the user name followed by `!demo`, for example `nurse1` / `nurse1!demo`.
Doctors and nurses dispense, pharmacists deposit, and administrators and technicians reset and disarm.

A typical run:
1. On the HMS, log in as doctor1 and order Insulin x5 for ADC-1. The order appears under "Waiting to be stocked".
2. On the panel, log in as pharmacist1, scan the order ID, answer Yes to the quantity check, open the door,
   press "Medication taken out / placed in", then close the door. The HMS shows the order as Stocked.
3. Log in as nurse1, press "Show order" and then "Dispense". Open the door, take the medication, and close the door. The HMS shows the order as Fulfilled.

## Where each module is (`adc/src/adc/`)

| PDF module | File | Class | Owns |
|---|---|---|---|
| 1 ADC Entire System | `cabinet.py` | `Cabinet` | builds and connects the modules in layer order, power on/off, the 50 ms tick |
| 1.1.1 Alarm Hardware | `hardware/alarm_hw.py` | `AlarmHardware` | the siren |
| 1.1.2 Keyboard interface | `hardware/keyboard.py` | `Keyboard` | keypad, badge and scanner input |
| 1.1.3 Display | `hardware/display.py` | `Display` | the screen; all wording is in `MESSAGES` |
| 1.1.4 Door | `hardware/door.py` | `Door` | latch, door and tamper sensors; a latched door stays shut |
| 1.1.5 Timer | `hardware/timer.py` | `Timer`, `SimulatedTimer` | the clock |
| 1.1 (added) Memory | `hardware/memory.py` | `Memory` | data that survives a power cut |
| 1.2.1 Authentication & Access Control | `software/auth.py` | `AuthManager` | HMS login check, failed-attempt count, lockout, session, service roles |
| 1.2.2 Inventory Manager | `software/inventory.py` | `InventoryManager` | compartments, deposit checks, quantity confirmation, inventory and capacity reports |
| 1.2.3 Event Logger | `software/event_logger.py` | `EventLogger` | the local log and `LOG_BATCH` uploads |
| 1.2.4 Communication with HMS | `software/hms_comm.py` | `HmsComm` | the TCP link, heartbeat, acknowledgements, outbox |
| 1.3.1 State Machine Manager | `behaviour/state_machine.py` | `StateMachine` | states and transitions, door locking, relock, door-open limit, session timeout, tamper lockout |
| 1.3.2 Dispensing Manager | `behaviour/dispensing.py` | `DispensingManager` | the user's inputs, order selection, dispense and deposit, order status to the HMS |
| 1.3.3 Alarm Policy | `behaviour/alarm_policy.py` | `AlarmPolicy` | raising alarms (cabinet forced open or damaged, door open too long) and disarming |
| (simulation) cabinet panel | `ui/panel.py`, `ui/panel.html` | `Panel` | the web page that stands in for the physical cabinet |

`adc/src/adc_main.py` starts a cabinet and its panel. It is not called `main.py` because the ADC tests put both `adc/src` and `monitor/src` on the import path and import the monitor's `main.py`.
The older command-line cabinet simulator that the monitor tests use is in `monitor/tests/adc_sim.py`.

## Requirements to code

| Requirement | Module | Where |
|---|---|---|
| FR-DSP-01 | Dispensing Manager | `DispensingManager._in_select_order`, `_in_dispense` (`WITHDRAW_ROLES`), `startDispense` |
| FR-DSP-02 | Dispensing Manager, Display | `DispensingManager.startDispense` shows `ORDER_DETAILS` on the `Display` |
| FR-SEC-01/03, NFR-SEC-01/05 | Auth & Access Control | `AuthManager.authenticate` (HMS check, 500 ms limit in `HmsComm.checkAuth`) |
| FR-SEC-02 | Auth, State Machine Manager | `AuthManager.authenticate` / `lockOut`, `StateMachine` (`LOCKED_OUT`) |
| FR-SEC-04/07 | Auth & Access Control | `AuthManager.verifyService` (`SERVICE_ROLES`), `clearLockedOut` |
| FR-SEC-05/08/09/10, NFR-SEC-02/04 | State Machine Manager | `StateMachine._timed_event` (5 min session, 10 s relock, 60 s door limit), `_on_door`, `_enter` |
| FR-SEC-06 | Alarm Policy | `AlarmPolicy._on_door`, `reportCondition`, `acknowledgeAlarm` |
| FR-IVT-01 to 05 | Inventory Manager | `InventoryManager.checkDeposit`, `confirmQuantity`, `recordDeposit`, `recordDispense` |
| FR-HMS-01/05 | Event Logger | `EventLogger.logEvent`, `flushLog` (`LOG_BATCH`) |
| FR-HMS-02, NFR-HMS-01 | Comm, Inventory | `HmsComm` (`ORDER_LIST` + ACK), `InventoryManager.reconcile` |
| FR-HMS-03 | Comm, Dispensing | `ORDER_STATUS` (Stocked / Fulfilled) from `DispensingManager` through `HmsComm` |
| FR-HMS-04 | Comm, Inventory | `InventoryManager._report` (`EVENT`, `INVENTORY` with `capacity_warning`) |
| FR-HMS-06 | Inventory | `InventoryManager` saved to `Memory` |
| FR-HMS-07 | Comm | `HmsComm._beat` (every 0.5 s) |

## Design decisions made while coding

- **Deposit mode.** The Dispense mode only allows doctors and nurses (FR-DSP-01 invariant), so stocking uses a mirrored `Deposit` mode with the same door rules.
- **Extra transitions.** The state machine adds logout and cancel (back to Idle or Authorized). It also lets a door closed before the medication was handled return to Dispense or Deposit, and an administrator or technician can reset the cabinet out of Invalid.
- **Staying logged in.** Where the PDF table goes back to Idle after a transaction (door closed and log sent, or the 10 s relock), the cabinet goes back to Authorized. The door locks, but the user stays logged in, because the PDF tracks the session separately (c_sessionState) and ends it only after 5 minutes of inactivity (FR-SEC-09). Every button press or door movement restarts that 5 minute timer.
- **Invalid order ID.** An order ID that is not stocked is refused (FR-DSP-01): the screen says so and the user stays logged in to try again. There is no alarm, because a mistyped ID is not a safety problem. The Invalid state is kept for a door left open more than 60 s, which needs an administrator or technician reset.
- **Hardware faults.** Pressing "Open door" while the latch is engaged does nothing to the cabinet: the door stays shut and the screen says what to do first (log in, choose an order, or wait for a reset). Forced entry and damage are simulated only by the tamper switch, which sounds the alarm and locks the cabinet out (FR-SEC-06, FR-SEC-08).
- **Alarm silencing.** Every alarm keeps sounding until an administrator or technician disarms it. Resetting a lockout does not silence the alarm.
- **HMS unreachable.** If the HMS cannot be reached, the login is refused and the cabinet stays locked. This does not count as a failed attempt.
- **Who checks passwords.** The HMS checks them (`AUTH_REQUEST` / `AUTH_RESULT`), and the cabinet never stores them. Passwords cross the LAN in plain TCP, the same trust level as the rest of the link.
- **Offline messages.** While the HMS is offline, outgoing messages wait in a persisted outbox. Each keeps its original time in `body.at` and is sent with a fresh frame time after reconnecting.

## Tests

`py -m unittest discover -s adc/tests` (20 tests, about 2 s). See `adc/tests/README.md`.
