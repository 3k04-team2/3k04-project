# Automated Dispensing Cabinet (adc)

Python 3.10 or newer, standard library only. On Windows the command is `py`.
This is the Deliverable 2 design implemented: the 4-variable model, the REQ tables and the module interfaces.

## Run the demo

1. From the repo root, start the HMS: `py monitor/src/main.py --sim-host`
2. In a second window start a cabinet: `py adc/src/adc_main.py`
   (a second cabinet: `py adc/src/adc_main.py --id ADC-2 --ui-port 8091`)
3. Open the cabinet panel at http://127.0.0.1:8090 and the HMS at http://127.0.0.1:8080

Options: `--id` (default ADC-1), `--hms-host`, `--port` (HMS cabinet port, default 9000), `--capacity` (default 16),
`--ui-port` (default 8090), `--data` (non-volatile data, default `adc/src/data/<id>`), `--lan`.

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

| PDF module | File | Class |
|---|---|---|
| 1 ADC Entire System | `cabinet.py` | `Cabinet` (builds and connects everything, power on/off) |
| 1.1.1 Alarm Hardware | `hardware/alarm_hw.py` | `AlarmHardware` |
| 1.1.2 Keyboard interface | `hardware/keyboard.py` | `Keyboard`, `InputEvent` |
| 1.1.3 Display | `hardware/display.py` | `Display` (all screen wording in `MESSAGES`) |
| 1.1.4 Door | `hardware/door.py` | `Door` (door, latch and tamper sensors) |
| 1.1.5 Timer | `hardware/timer.py` | `Timer`, `SimulatedTimer` |
| 1.1 (added) non-volatile storage | `hardware/nvram.py` | `Nvram` |
| 1.2.1 Authentication & Access Control | `software/auth.py` | `AuthManager` |
| 1.2.2 Inventory Manager | `software/inventory.py` | `InventoryManager` |
| 1.2.3 Event Logger | `software/event_logger.py` | `EventLogger` |
| 1.2.4 Communication with HMS | `software/hms_comm.py` | `HmsComm` |
| 1.3.1 State Machine Manager | `behaviour/state_machine.py` | `StateMachine` |
| 1.3.2 Dispensing Manager | `behaviour/dispensing.py` | `DispensingManager` |
| 1.3.3 Alarm Policy | `behaviour/alarm_policy.py` | `AlarmPolicy` |
| (simulation) cabinet panel | `ui/panel.py`, `ui/panel.html` | `Panel` |

`adc/src/adc_main.py` starts a cabinet and its panel. It is not called `main.py` because the monitor's tests put `adc/src` on the import path and import the monitor's `main.py`.
`adc/src/adc_sim.py` is the older command-line cabinet simulator, which the monitor tests still use.

## Requirements to code

| Requirement | Where |
|---|---|
| FR-DSP-01/02, NFR-DSP-01 | `DispensingManager._in_select_order`, `_in_dispense`, `startDispense`, `confirmRetrieval` |
| FR-SEC-01/02/03, NFR-SEC-01/05 | `AuthManager.authenticate` (HMS check, 500 ms limit in `HmsComm.checkAuth`), `DispensingManager._in_login` |
| FR-SEC-04 | `DispensingManager._in_reset`, `AuthManager.verifyService` / `clearLockedOut` |
| FR-SEC-05/06/07/08, NFR-SEC-02/04 | `AlarmPolicy`, `DispensingManager._on_door`, `_security`, `handleTimeout` (50 ms tick in `Cabinet`) |
| FR-SEC-09, FR-SEC-10 | `DispensingManager.handleTimeout` (5 min session, 10 s relock) |
| FR-IVT-01/05 | `InventoryManager` |
| FR-IVT-02/03/04 | `DispensingManager._in_scan_deposit`, `_in_confirm_quantity` |
| FR-HMS-01/05 | `EventLogger` (`LOG_BATCH`), `EVENT` in `DispensingManager._record_movement`, `AlarmPolicy.acknowledgeAlarm` |
| FR-HMS-02, NFR-HMS-01 | `HmsComm` (`ORDER_LIST` + ACK), `DispensingManager.handleNewOrder`, `InventoryManager.reconcile` |
| FR-HMS-03 | `ORDER_STATUS` (Stocked / Fulfilled) in `DispensingManager` |
| FR-HMS-04 | `INVENTORY` with `capacity_warning` in `DispensingManager._send_inventory` |
| FR-HMS-06 | `Nvram` + `InventoryManager`, `AuthManager` (lockout), `EventLogger`, `HmsComm` outbox |
| FR-HMS-07 | `HmsComm._beat` (every 0.5 s) |

## Design decisions made while coding

- **Deposit mode.** The Dispense mode only allows doctors and nurses (FR-DSP-01 invariant), so stocking uses a mirrored `Deposit` mode with the same door rules.
- **Extra transitions.** The state machine adds logout and cancel (back to Idle or Authorized). It also lets a door closed before the medication was handled return to Dispense or Deposit, and an administrator or technician can reset the cabinet out of Invalid.
- **Staying logged in.** Where the PDF table goes back to Idle after a transaction (door closed and log sent, or the 10 s relock), the cabinet goes back to Authorized. The door locks, but the user stays logged in, because the PDF tracks the session separately (c_sessionState) and ends it only after 5 minutes of inactivity (FR-SEC-09). Every button press or door movement restarts that 5 minute timer.
- **Invalid order ID.** An order ID that is not stocked is refused (FR-DSP-01): the screen says so and the user stays logged in to try again. There is no alarm, because a mistyped ID is not a safety problem. The Invalid state is kept for a door left open more than 60 s, which needs an administrator or technician reset.
- **Alarm silencing.** Every alarm keeps sounding until an administrator or technician disarms it. Resetting a lockout does not silence the alarm.
- **HMS unreachable.** If the HMS cannot be reached, the login is refused and the cabinet stays locked. This does not count as a failed attempt.
- **Who checks passwords.** The HMS checks them (`AUTH_REQUEST` / `AUTH_RESULT`), and the cabinet never stores them. Passwords cross the LAN in plain TCP, the same trust level as the rest of the link.
- **Offline messages.** While the HMS is offline, outgoing messages wait in a persisted outbox. Each keeps its original time in `body.at` and is sent with a fresh frame time after reconnecting.

## Tests

`py -m unittest discover -s adc/tests` (19 tests, about 2 s). See `adc/tests/README.md`.
