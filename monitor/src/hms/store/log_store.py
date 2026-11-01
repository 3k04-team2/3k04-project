"""M9 Log Store.

Secret: the storage engine (SQLite now), the encryption of entries, the hash
chain and the retention rule. The database lives in memory and is saved as one
file through the Host Platform (the only module that touches the disk).
Uses: M12 Host Platform.

Encryption: each entry's data is encrypted with a keystream built from
HMAC-SHA256 plus an authentication tag (standard library only). A production
system would swap in AES-GCM here; nothing outside this module would change.
The key comes from the HMS_LOG_KEY environment variable; a demo key is used
when it is not set.
"""

import csv
import hashlib
import hmac
import io
import json
import os
import sqlite3
import threading

from hms.host import DiskError, FileMissing

H0 = "0" * 64
RETENTION_SECONDS = 365 * 86400


class RetentionViolation(Exception):
    pass


class NoMatches(Exception):
    pass


def _keystream(key, nonce, n):
    out = b""
    counter = 0
    while len(out) < n:
        out += hmac.new(key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest()
        counter += 1
    return out[:n]


def encrypt(key, plain):
    nonce = os.urandom(16)
    ks = _keystream(key, nonce, len(plain))
    ct = bytes(a ^ b for a, b in zip(plain, ks))
    tag = hmac.new(key, b"tag" + nonce + ct, hashlib.sha256).digest()
    return nonce + ct + tag


def decrypt(key, blob):
    if len(blob) < 48:
        raise ValueError("too short")
    nonce, ct, tag = blob[:16], blob[16:-32], blob[-32:]
    good = hmac.new(key, b"tag" + nonce + ct, hashlib.sha256).digest()
    if not hmac.compare_digest(tag, good):
        raise ValueError("authentication failed")
    ks = _keystream(key, nonce, len(ct))
    return bytes(a ^ b for a, b in zip(ct, ks))


def entry_hash(prev, time, adc, user, type_, data):
    text = "%s|%r|%s|%s|%s|%s" % (prev, time, adc, user, type_, data)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class LogStore:
    def __init__(self, host, data_dir, passphrase=None):
        self._host = host
        self._dir = data_dir
        self._path = os.path.join(data_dir, "log.db")
        self._index = os.path.join(data_dir, "log.backups.json")
        self._key = hashlib.sha256(
            (passphrase or os.environ.get("HMS_LOG_KEY") or "demo-key-change-me").encode()).digest()
        self._lock = threading.RLock()
        self._dirty = False
        self.damaged = False
        self._open_database()

    # ---- storage ------------------------------------------------------
    def _new_db(self):
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        conn.execute("CREATE TABLE IF NOT EXISTS log(id INTEGER PRIMARY KEY AUTOINCREMENT,"
                     " time REAL, adc TEXT, user TEXT, type TEXT, data BLOB, hash TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
        return conn

    def _open_database(self):
        self._conn = self._new_db()
        if self._host.exists(self._path):
            try:
                conn = sqlite3.connect(":memory:", check_same_thread=False)
                conn.deserialize(self._host.read_file(self._path))
                conn.execute("SELECT count(*) FROM log").fetchone()
                self._conn = conn
            except (sqlite3.DatabaseError, FileMissing):
                self.damaged = True

    def _persist(self):
        try:
            self._host.write_file(self._path, self._conn.serialize())
            self._dirty = False
        except DiskError:
            self._dirty = True

    def _base(self):
        row = self._conn.execute("SELECT v FROM meta WHERE k='base'").fetchone()
        return row[0] if row else H0

    # ---- operations ---------------------------------------------------
    def append(self, entry):
        """entry: dict with adc, user, type, data (and optionally time). Returns the id."""
        with self._lock:
            time = float(entry.get("time", self._host.now()))
            adc = str(entry.get("adc", ""))
            user = str(entry.get("user", ""))
            type_ = str(entry.get("type", ""))
            data = entry.get("data", "")
            if not isinstance(data, str):
                data = json.dumps(data, sort_keys=True)
            row = self._conn.execute("SELECT hash FROM log ORDER BY id DESC LIMIT 1").fetchone()
            prev = row[0] if row else self._base()
            h = entry_hash(prev, time, adc, user, type_, data)
            cur = self._conn.execute(
                "INSERT INTO log(time, adc, user, type, data, hash) VALUES (?,?,?,?,?,?)",
                (time, adc, user, type_, encrypt(self._key, data.encode("utf-8")), h))
            self._conn.commit()
            self._persist()
            return cur.lastrowid

    def _rows(self, flt, sort):
        flt = flt or {}
        where, args = [], []
        for col in ("adc", "user", "type"):
            if flt.get(col):
                where.append(col + "=?")
                args.append(flt[col])
        if flt.get("since") is not None:
            where.append("time>=?")
            args.append(flt["since"])
        if flt.get("until") is not None:
            where.append("time<=?")
            args.append(flt["until"])
        sql = "SELECT id,time,adc,user,type,data,hash FROM log"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY time %s, id %s" % (("DESC", "DESC") if sort == "desc" else ("ASC", "ASC"))
        return self._conn.execute(sql, args).fetchall()

    def query(self, flt=None, sort="asc"):
        with self._lock:
            out = []
            for id_, time, adc, user, type_, blob, h in self._rows(flt, sort):
                try:
                    data = decrypt(self._key, blob).decode("utf-8")
                except ValueError:
                    data = "<cannot be decrypted>"
                out.append({"id": id_, "time": time, "adc": adc, "user": user,
                            "type": type_, "data": data, "hash": h})
            return out

    def verify(self):
        """Ids of entries whose hash does not match their content."""
        with self._lock:
            bad = []
            prev = self._base()
            for id_, time, adc, user, type_, blob, h in self._rows(None, "asc"):
                try:
                    data = decrypt(self._key, blob).decode("utf-8")
                    ok = entry_hash(prev, time, adc, user, type_, data) == h
                except ValueError:
                    ok = False
                if not ok:
                    bad.append(id_)
                prev = h
            return bad

    def exportCsv(self, flt=None, user="system"):
        with self._lock:
            rows = self.query(flt)
            if not rows:
                raise NoMatches("no entries match")
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["id", "time", "adc", "user", "type", "data"])
            for r in rows:
                w.writerow([r["id"], r["time"], r["adc"], r["user"], r["type"], r["data"]])
            path = os.path.join(self._dir, "exports", "log-export-%d.csv" % int(self._host.now()))
            self._host.write_file(path, buf.getvalue().encode("utf-8"))
            self.append({"user": user, "type": "EXPORT", "data": "%d entries to %s" % (len(rows), path)})
            return path

    def delete(self, before):
        with self._lock:
            if before > self._host.now() - RETENTION_SECONDS:
                self.append({"type": "DELETE_REFUSED", "data": "entries younger than 365 days"})
                raise RetentionViolation("entries under 365 days old cannot be deleted")
            last = self._conn.execute(
                "SELECT hash FROM log WHERE time<? ORDER BY id DESC LIMIT 1", (before,)).fetchone()
            if last:
                self._conn.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('base',?)", (last[0],))
            self._conn.execute("DELETE FROM log WHERE time<?", (before,))
            self._conn.commit()
            self._persist()

    def backup(self):
        with self._lock:
            self._persist()
            if self._dirty:
                raise DiskError("log could not be saved")
            dst = os.path.join(self._dir, "backups", "log-%d.db" % int(self._host.now()))
            self._host.copy_file(self._path, dst)
            self._record_backup(dst)
            return dst

    def _record_backup(self, dst):
        try:
            index = json.loads(self._host.read_file(self._index))
        except (FileMissing, ValueError):
            index = []
        index.append({"path": dst, "time": self._host.now()})
        self._host.write_file(self._index, json.dumps(index).encode())

    def restoreLatest(self):
        """Replace the data with the newest backup. Returns the backup's age in seconds."""
        with self._lock:
            index = json.loads(self._host.read_file(self._index))
            latest = index[-1]
            self._host.copy_file(latest["path"], self._path)
            self.damaged = False
            self._open_database()
            return self._host.now() - latest["time"]

    def health(self):
        with self._lock:
            try:
                self._conn.execute("SELECT 1").fetchone()
            except sqlite3.Error:
                return False
            if self._dirty:
                self._persist()
            return not self._dirty
