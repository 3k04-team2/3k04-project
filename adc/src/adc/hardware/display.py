"""1.1.3 Display.

Secret: how text reaches the cabinet screen, the wording of every message and
how long it stays. Callers name a message by its ID and pass its values.
"""

import threading

MESSAGES = {
    "IDLE": "Cabinet locked. Please log in.",
    "WELCOME": "Welcome {user} ({role}).",
    "ACCESS_DENIED": "Access denied.",
    "LOGIN_WARNING": "Warning: {count} failed login attempts. The cabinet locks out after 5.",
    "HMS_UNREACHABLE": "Cannot reach the HMS to check the login. The cabinet stays locked.",
    "SERVICE_UNREACHABLE": "Cannot reach the HMS to check the administrator or technician password. "
                           "Reconnect the network and try again.",
    "ROLE_NOT_ALLOWED": "{role} accounts cannot open the cabinet. Use Reset or Disarm.",
    "LOCKED_OUT": "LOCKED OUT. An administrator or technician must reset the cabinet.",
    "MAINTENANCE": "Cabinet in maintenance mode.",
    "ORDER_DETAILS": "Order {order}: {medication} x{quantity}, compartment {compartment}. Press Dispense to open.",
    "INVALID_ORDER": "Order {order} is not stocked in this cabinet. Check the order ID and try again.",
    "DOOR_UNLOCKED": "Door unlocked. Open it within 10 seconds.",
    "TAKE_MEDICATION": "Take {medication} x{quantity} from compartment {compartment}, then close the door.",
    "PLACE_MEDICATION": "Place {medication} x{quantity} in compartment {compartment}, then close the door.",
    "CLOSE_DOOR": "Done. Please close the door.",
    "RELOCKED": "The door was not opened within 10 seconds and has locked again.",
    "CANCELLED": "Cancelled.",
    "SESSION_ENDED": "Session ended.",
    "SESSION_TIMEOUT": "Session ended after 5 minutes without activity.",
    "WRONG_ADC": "Order {order} is for {destination}, not this cabinet ({adc}). Deposit blocked.",
    "NOT_ASSIGNED": "Order {order} is not on this cabinet's order list. Deposit blocked.",
    "ALREADY_STOCKED": "Order {order} is already stocked in compartment {compartment}.",
    "CABINET_FULL": "Cabinet is full ({capacity} compartments). Deposit blocked.",
    "CONFIRM_QUANTITY": "Count the package: is it {medication} x{quantity} for order {order}?",
    "QUANTITY_NOT_CONFIRMED": "Quantity not confirmed for order {order}. Deposit blocked.",
    "DEPOSIT_COMPLETE": "Order {order} stocked in compartment {compartment}.",
    "DISPENSE_COMPLETE": "Order {order} dispensed.",
    "INVALID_STATE": "INVALID STATE: {reason}. An administrator or technician must reset the cabinet.",
    "ALARM_DISARMED": "Alarm disarmed by {user}.",
    "RESET_DONE": "Cabinet reset by {user}.",
}


class Display:
    def __init__(self, timer):
        self._timer = timer
        self._lock = threading.Lock()
        self._message = ""
        self._until = None
        self._blocking = None

    @staticmethod
    def _text(messageID, params):
        return MESSAGES[messageID].format(**params)

    def showMessage(self, messageID, duration=None, **params):
        """duration in seconds; None keeps the message until replaced."""
        text = self._text(messageID, params)
        with self._lock:
            self._message = text
            self._until = None if duration is None else self._timer.monotonic() + duration
        return text

    def showBlockingMessage(self, messageID, **params):
        text = self._text(messageID, params)
        with self._lock:
            self._blocking = text
        return text

    def clearBlockingMessage(self):
        with self._lock:
            self._blocking = None

    def clearDisplay(self):
        with self._lock:
            self._message = ""
            self._until = None
            self._blocking = None

    def isBlocked(self):
        return self._blocking is not None

    def read(self):
        with self._lock:
            if self._until is not None and self._timer.monotonic() >= self._until:
                self._message, self._until = "", None
            return {"message": self._message, "blocking": self._blocking}
