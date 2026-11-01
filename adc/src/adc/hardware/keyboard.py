"""1.1.2 Keyboard Interface.

Secret: which physical devices produce user input (keypad, touch panel, badge
or barcode scanner). Every input reaches the rest of the cabinet as one
InputEvent passed to the registered handler (interrupt style, no polling).
"""

KINDS = {
    "login",            # data: user, password            (badge/PIN)
    "logout",
    "select_order",     # data: order                     (keypad or barcode)
    "dispense",         # confirm the order shown on the screen
    "scan_deposit",     # data: order, destination        (pharmacist scans a package)
    "confirm_quantity", # data: matches (true/false)
    "compartment_done", # compartment sensor: medication taken out / placed in
    "cancel",
    "dismiss",          # close a blocking message
    "reset",            # data: user, password            (administrator/technician)
    "disarm",           # data: user, password, notes     (administrator/technician)
    "maintenance",      # data: user, password, enable    (technician)
}


class InputEvent:
    def __init__(self, kind, data=None):
        self.kind = kind
        self.data = dict(data or {})

    def __repr__(self):
        safe = {k: ("***" if k == "password" else v) for k, v in self.data.items()}
        return "InputEvent(%s %s)" % (self.kind, safe)


class Keyboard:
    def __init__(self):
        self._handler = None
        self._last = None

    def registerInputHandler(self, handler):
        """handler(InputEvent) -> result dict."""
        self._handler = handler

    def clearInput(self):
        self._last = None

    def press(self, kind, **data):
        """Called by the input device (the web panel in this build)."""
        if kind not in KINDS:
            raise ValueError("unknown input " + kind)
        self._last = InputEvent(kind, data)
        if self._handler is None:
            return {"ok": False, "message": "the cabinet is not ready"}
        return self._handler(self._last)
