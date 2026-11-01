"""M10 State Store.

Secret: how operational state (orders, inventory, users, configuration) is
saved, backed up and restored (one JSON file now). Every save is written to
disk before it returns. Uses: M12 Host Platform.
"""

import json
import os
import threading

from hms.host import DiskError, FileMissing


class StateStore:
    def __init__(self, host, data_dir):
        self._host = host
        self._dir = data_dir
        self._path = os.path.join(data_dir, "state.json")
        self._index = os.path.join(data_dir, "state.backups.json")
        self._lock = threading.RLock()
        self._ok = True
        self.damaged = False
        self._data = {}
        if host.exists(self._path):
            try:
                self._data = json.loads(host.read_file(self._path))
                if not isinstance(self._data, dict):
                    raise ValueError("not an object")
            except (ValueError, FileMissing):
                self._data = {}
                self.damaged = True

    def save(self, key, record):
        with self._lock:
            self._data[key] = record
            try:
                self._host.write_file(self._path, json.dumps(self._data, sort_keys=True).encode())
                self._ok = True
            except DiskError:
                self._ok = False
                raise

    def load(self, key, default=None):
        with self._lock:
            return self._data.get(key, default)

    def backup(self):
        with self._lock:
            dst = os.path.join(self._dir, "backups", "state-%d.json" % int(self._host.now()))
            self._host.write_file(self._path, json.dumps(self._data, sort_keys=True).encode())
            self._host.copy_file(self._path, dst)
            try:
                index = json.loads(self._host.read_file(self._index))
            except (FileMissing, ValueError):
                index = []
            index.append({"path": dst, "time": self._host.now()})
            self._host.write_file(self._index, json.dumps(index).encode())
            return dst

    def restoreLatest(self):
        """Replace the data with the newest backup. Returns the backup's age in seconds."""
        with self._lock:
            index = json.loads(self._host.read_file(self._index))
            latest = index[-1]
            self._data = json.loads(self._host.read_file(latest["path"]))
            self._host.write_file(self._path, json.dumps(self._data, sort_keys=True).encode())
            self.damaged = False
            return self._host.now() - latest["time"]

    def health(self):
        return self._ok
