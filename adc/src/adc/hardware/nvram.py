"""Non-volatile storage (part of 1.1 Hardware Hiding).

Secret: where data that must survive a power loss is kept (one JSON file per
record in a folder now; NVRAM or flash on the real cabinet). A write is on
disk before it returns (FR-HMS-06, NAT-05).
"""

import json
import os
import threading


class Nvram:
    def __init__(self, folder):
        self._folder = folder
        self._lock = threading.Lock()
        os.makedirs(folder, exist_ok=True)

    def _path(self, name):
        return os.path.join(self._folder, name + ".json")

    def read(self, name, default=None):
        try:
            with open(self._path(name), "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return default
        except ValueError:
            os.replace(self._path(name), self._path(name) + ".damaged")
            return default

    def write(self, name, obj):
        path = self._path(name)
        tmp = path + ".tmp"
        data = json.dumps(obj, sort_keys=True).encode("utf-8")
        with self._lock:
            with open(tmp, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
