"""Same-host session leases with revocation and expiring request reservations."""
from contextlib import contextmanager
from pathlib import Path
import sqlite3
from time import time
from uuid import uuid4


class SharedSessionStore:
    def __init__(self, path, capacity, idle_seconds, *, clock=time, request_seconds=180):
        self.path = str(Path(path).resolve())
        self.capacity, self.idle_seconds = capacity, idle_seconds
        self.clock, self.request_seconds = clock, request_seconds
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path, timeout=5) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
        with self.transaction() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS login_leases (sid TEXT PRIMARY KEY, user_id INTEGER NOT NULL, touched REAL NOT NULL, beacon TEXT UNIQUE NOT NULL)")
            connection.execute("CREATE TABLE IF NOT EXISTS request_leases (rid TEXT PRIMARY KEY, sid TEXT NOT NULL, expires REAL NOT NULL)")
            connection.execute("CREATE INDEX IF NOT EXISTS ix_request_sid ON request_leases (sid, expires)")

    @contextmanager
    def transaction(self):
        connection = sqlite3.connect(self.path, timeout=5)
        try:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
        finally:
            connection.close()

    def _reap(self, connection):
        now = self.clock()
        connection.execute("DELETE FROM request_leases WHERE expires<=?", (now,))
        return connection.execute("DELETE FROM login_leases WHERE touched<=? AND NOT EXISTS (SELECT 1 FROM request_leases WHERE request_leases.sid=login_leases.sid)", (now - self.idle_seconds,)).rowcount

    def allocate(self, sid, user_id, beacon):
        with self.transaction() as connection:
            self._reap(connection)
            if connection.execute("SELECT COUNT(*) FROM login_leases").fetchone()[0] >= self.capacity:
                return False
            connection.execute("INSERT INTO login_leases VALUES (?, ?, ?, ?)", (sid, user_id, self.clock(), beacon))
            return True

    def acquire(self, sid):
        with self.transaction() as connection:
            self._reap(connection)
            row = connection.execute("SELECT user_id FROM login_leases WHERE sid=?", (sid,)).fetchone()
            if not row:
                return None
            rid = uuid4().hex
            connection.execute("UPDATE login_leases SET touched=? WHERE sid=?", (self.clock(), sid))
            connection.execute("INSERT INTO request_leases VALUES (?, ?, ?)", (rid, sid, self.clock() + self.request_seconds))
            return row[0], rid

    def finish(self, sid, rid):
        with self.transaction() as connection:
            connection.execute("DELETE FROM request_leases WHERE rid=? AND sid=?", (rid, sid))
            connection.execute("UPDATE login_leases SET touched=? WHERE sid=?", (self.clock(), sid))

    def user(self, sid, *, touch=False):
        if not touch:
            connection = sqlite3.connect(self.path, timeout=5)
            try:
                now = self.clock()
                row = connection.execute("SELECT user_id FROM login_leases WHERE sid=? AND (touched>? OR EXISTS (SELECT 1 FROM request_leases WHERE request_leases.sid=login_leases.sid AND expires>?))",
                                         (sid, now - self.idle_seconds, now)).fetchone()
                return row[0] if row else None
            finally:
                connection.close()
        with self.transaction() as connection:
            self._reap(connection)
            row = connection.execute("SELECT user_id FROM login_leases WHERE sid=?", (sid,)).fetchone()
            if row and touch:
                connection.execute("UPDATE login_leases SET touched=? WHERE sid=?", (self.clock(), sid))
            return row[0] if row else None

    def release(self, sid=None, user_id=None, beacon=None):
        with self.transaction() as connection:
            if beacon is not None:
                row = connection.execute("SELECT sid FROM login_leases WHERE beacon=?", (beacon,)).fetchone()
                sid = row[0] if row else None
            row = connection.execute("SELECT user_id FROM login_leases WHERE sid=?", (sid,)).fetchone()
            if not row or (user_id is not None and row[0] != user_id):
                return False
            connection.execute("DELETE FROM request_leases WHERE sid=?", (sid,))
            connection.execute("DELETE FROM login_leases WHERE sid=?", (sid,))
            return True

    def reap(self):
        with self.transaction() as connection:
            return self._reap(connection)

    def snapshot(self):
        with self.transaction() as connection:
            self._reap(connection)
            users = dict(connection.execute("SELECT user_id, COUNT(*) FROM login_leases GROUP BY user_id").fetchall())
            active = connection.execute("SELECT COUNT(*) FROM request_leases").fetchone()[0]
            return users, active
