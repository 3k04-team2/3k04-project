"""M12 Host Platform.

Secret: how the host computer provides the clock, the speaker and the files.
Nothing else in the HMS reads the clock, makes a sound or touches the disk;
every other module asks this one. Two implementations sit behind one interface:
HostPlatform (the real computer) and SimulatedHost (a fake clock, a recorded
speaker and in-memory files), so timing and alarm tones can be demonstrated and
tested without waiting.

Uses: no other module.
"""

import os
import shutil
import threading
import time


class SpeakerUnavailable(Exception):
    """The host has no way to make a sound."""


class DiskError(Exception):
    """A file could not be written or copied (disk full, not writable)."""


class FileMissing(Exception):
    """A file that was asked for does not exist."""


class Host:
    """The interface every implementation provides.

    now()               -> UTC time in seconds since 1970 (host clock, kept in sync by NTP)
    monotonic()         -> seconds since an arbitrary start; never goes backward
    set_tone(on)        -> turns the audible alarm tone on or off; safe to repeat
    tone_is_on()        -> whether the tone is on now
    write_file(p, data) -> data is on disk before this returns, or DiskError
    read_file(p)        -> the bytes, or FileMissing
    exists(p)           -> True or False
    copy_file(src, dst) -> copies a file (backups), or FileMissing / DiskError
    """

    def now(self):
        raise NotImplementedError

    def monotonic(self):
        raise NotImplementedError

    def set_tone(self, on):
        raise NotImplementedError

    def tone_is_on(self):
        raise NotImplementedError

    def write_file(self, path, data):
        raise NotImplementedError

    def read_file(self, path):
        raise NotImplementedError

    def exists(self, path):
        raise NotImplementedError

    def copy_file(self, src, dst):
        raise NotImplementedError


class HostPlatform(Host):
    """The real computer."""

    def __init__(self):
        self._tone_on = False
        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.Lock()

    def now(self):
        return time.time()

    def monotonic(self):
        return time.monotonic()

    def set_tone(self, on):
        with self._lock:
            if on == self._tone_on:
                return
            if on:
                beep = self._beeper()
                self._stop.clear()
                self._thread = threading.Thread(target=self._sound, args=(beep,), daemon=True)
                self._thread.start()
            else:
                self._stop.set()
            self._tone_on = on

    def tone_is_on(self):
        return self._tone_on

    def write_file(self, path, data):
        tmp = path + ".tmp"
        try:
            folder = os.path.dirname(path)
            if folder:
                os.makedirs(folder, exist_ok=True)
            with open(tmp, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except OSError as e:
            raise DiskError(str(e))

    def read_file(self, path):
        try:
            with open(path, "rb") as f:
                return f.read()
        except FileNotFoundError:
            raise FileMissing(path)

    def exists(self, path):
        return os.path.exists(path)

    def copy_file(self, src, dst):
        if not os.path.exists(src):
            raise FileMissing(src)
        try:
            folder = os.path.dirname(dst)
            if folder:
                os.makedirs(folder, exist_ok=True)
            shutil.copyfile(src, dst)
        except OSError as e:
            raise DiskError(str(e))

    @staticmethod
    def _beeper():
        try:
            import winsound
            return lambda: winsound.Beep(880, 300)
        except ImportError:
            pass
        if os.isatty(1):
            return lambda: (print("\a", end="", flush=True), time.sleep(0.3))
        raise SpeakerUnavailable("no sound device")

    def _sound(self, beep):
        while not self._stop.is_set():
            beep()
            self._stop.wait(0.2)


class SimulatedHost(Host):
    """A fake computer for tests and demos. Time only moves when told to."""

    def __init__(self, start=1_700_000_000.0):
        self._t = start
        self._mono = 0.0
        self._tone_on = False
        self.tone_log = []          # (time, on) every time the tone changes
        self.files = {}             # path -> bytes
        self.speaker_broken = False
        self.disk_full = False

    def advance(self, seconds):
        self._t += seconds
        self._mono += seconds

    def now(self):
        return self._t

    def monotonic(self):
        return self._mono

    def set_tone(self, on):
        if self.speaker_broken:
            raise SpeakerUnavailable("simulated speaker failure")
        if on != self._tone_on:
            self._tone_on = on
            self.tone_log.append((self._t, on))

    def tone_is_on(self):
        return self._tone_on

    def write_file(self, path, data):
        if self.disk_full:
            raise DiskError("simulated disk full")
        self.files[path] = bytes(data)

    def read_file(self, path):
        if path not in self.files:
            raise FileMissing(path)
        return self.files[path]

    def exists(self, path):
        return path in self.files

    def copy_file(self, src, dst):
        if src not in self.files:
            raise FileMissing(src)
        if self.disk_full:
            raise DiskError("simulated disk full")
        self.files[dst] = self.files[src]
