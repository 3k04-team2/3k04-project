# ADC tests

From the repo root: `py -m unittest discover -s adc/tests` (18 tests, about 2 s).

`test_adc.py` has three groups:

- `CabinetTests` run the whole cabinet with a simulated clock and a fake HMS link, so timing rules are checked without waiting:
  - deposit then dispense
  - roles (pharmacists cannot dispense, nurses cannot deposit)
  - the 10 s relock and the 60 s door limit (Invalid, alarm, reset, disarm with notes and time to disarm)
  - the warning on the 3rd failed login and lockout on the 5th, and the reset
  - forced entry and tamper
  - the three deposit blocks and the full cabinet
  - an invalid order ID being refused without locking the cabinet
  - inventory and lockout surviving a power cut
  - the 5 minute session timeout, and any interaction restarting it
  - the user staying logged in after a deposit, a dispense or a relock
  - the HMS being unreachable
  - an order cancelled by the HMS
  - the web panel API
- `HmsCommTests` check that messages queue up while offline and are still there after a restart.
- `HmsRoundTripTests` start the real HMS (`monitor/src/main.py`) and a real cabinet over TCP. The run checks each login with the HMS, syncs an order to the cabinet, stocks it and dispenses it. Afterwards it checks that the HMS shows the order as Fulfilled and that its log chain is intact.
