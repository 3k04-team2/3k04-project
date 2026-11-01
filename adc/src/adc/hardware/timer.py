"""1.1.5 Timer.

Secret: how the cabinet reads time (the hardware clock now). Other modules
start and stop timings here and never read the clock themselves, so tests can
use SimulatedTimer and move time forward without waiting.
"""

import itertools
import threading
import time


class Timer:
    def __init__(self):
        self._starts = {}
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    def _mono(self):
        return time.monotonic()

    def timeNow(self):
        """Wall-clock time in seconds since 1970 (used for timestamps)."""
        return time.time()

    def monotonic(self):
        return self._mono()

    def startTiming(self):
        with self._lock:
            tid = next(self._ids)
            self._starts[tid] = self._mono()
            return tid

    def elapsed(self, timingID):
        """Seconds since startTiming, without ending it; None if unknown."""
        start = self._starts.get(timingID)
        return None if start is None else self._mono() - start

    def endTiming(self, timingID):
        with self._lock:
            start = self._starts.pop(timingID, None)
        return None if start is None else self._mono() - start

    def cancelTiming(self, timingID):
        with self._lock:
            self._starts.pop(timingID, None)


class SimulatedTimer(Timer):
    """Time moves only when advance() is called."""

    def __init__(self, start=None):
        Timer.__init__(self)
        self._t = time.time() if start is None else start
        self._m = 0.0

    def _mono(self):
        return self._m

    def timeNow(self):
        return self._t

    def advance(self, seconds):
        self._t += seconds
        self._m += seconds
