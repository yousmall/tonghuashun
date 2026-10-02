"""Bounded same-host SQLite public fact cache with process-safe leases.

Only adapter-returned public facts may enter this store. Model outputs,
profiles and holdings must never be persisted here.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import time
from uuid import uuid4

from backend.app.models import FactRecord
from backend.app.services.provider_errors import ProviderCallError


class SqlitePublicFactStore:
    def __init__(self, path: str, *, max_entries: int = 128, lease_seconds: float = 35):
        self.path = str(Path(path).resolve())
        self.max_entries = max_entries
        self.lease_seconds = lease_seconds
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("CREATE TABLE IF NOT EXISTS public_facts (key TEXT PRIMARY KEY, payload TEXT NOT NULL, updated REAL NOT NULL, refreshed INTEGER NOT NULL)")
            connection.execute("CREATE TABLE IF NOT EXISTS fact_leases (key TEXT PRIMARY KEY, owner TEXT NOT NULL, expires REAL NOT NULL)")
            connection.execute("CREATE TABLE IF NOT EXISTS public_failures (key TEXT PRIMARY KEY, code TEXT NOT NULL, status INTEGER, updated REAL NOT NULL)")

    def connect(self):
        return sqlite3.connect(self.path, timeout=1)

    @staticmethod
    def key(query_key: str) -> str:
        return hashlib.sha256(query_key.encode()).hexdigest()

    def read(self, key: str):
        with self.connect() as connection:
            row = connection.execute("SELECT payload, updated, refreshed FROM public_facts WHERE key=?", (self.key(key),)).fetchone()
        if row is None:
            return None
        try:
            return [FactRecord.model_validate(item) for item in json.loads(row[0])], row[1], bool(row[2])
        except (ValueError, TypeError):
            return None

    def claim(self, key: str) -> str | None:
        owner, now = uuid4().hex, time.time()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM fact_leases WHERE expires<=?", (now,))
            connection.execute("INSERT OR IGNORE INTO fact_leases VALUES (?, ?, ?)",
                               (self.key(key), owner, now + self.lease_seconds))
            row = connection.execute("SELECT owner FROM fact_leases WHERE key=?", (self.key(key),)).fetchone()
        return owner if row and row[0] == owner else None

    def permission_failure(self, key):
        with self.connect() as connection:
            row = connection.execute("SELECT code, status, updated FROM public_failures WHERE key=?", (self.key(key),)).fetchone()
        if row and 0 <= time.time() - row[2] < 15 and row[0] in {"AUTHENTICATION_REJECTED", "CAPABILITY_FORBIDDEN"}:
            return ProviderCallError("该查询的问财授权暂未通过，请核对对应能力的账号授权。", code=row[0], status_code=row[1])
        return None

    def publish_permission_failure(self, key, owner, error):
        if error.code not in {"AUTHENTICATION_REJECTED", "CAPABILITY_FORBIDDEN"}:
            return
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            lease = connection.execute("SELECT owner, expires FROM fact_leases WHERE key=?", (self.key(key),)).fetchone()
            if lease and lease[0] == owner and lease[1] > time.time():
                connection.execute("INSERT OR REPLACE INTO public_failures VALUES (?, ?, ?, ?)",
                                   (self.key(key), error.code, error.status_code, time.time()))
                connection.execute("DELETE FROM public_failures WHERE key IN (SELECT key FROM public_failures ORDER BY updated DESC LIMIT -1 OFFSET ?)", (self.max_entries,))

    def publish(self, key: str, owner: str, facts: list[FactRecord], refreshed: bool):
        payload = json.dumps([fact.model_dump(mode="json") for fact in facts], ensure_ascii=False)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            lease = connection.execute("SELECT owner, expires FROM fact_leases WHERE key=?", (self.key(key),)).fetchone()
            if not lease or lease[0] != owner or lease[1] <= time.time():
                return  # expired writers cannot overwrite newer evidence
            connection.execute("INSERT OR REPLACE INTO public_facts VALUES (?, ?, ?, ?)",
                               (self.key(key), payload, time.time(), int(refreshed)))
            connection.execute("DELETE FROM public_failures WHERE key=?", (self.key(key),))
            connection.execute("DELETE FROM public_facts WHERE key IN (SELECT key FROM public_facts ORDER BY updated DESC LIMIT -1 OFFSET ?)", (self.max_entries,))

    def release(self, key: str, owner: str):
        with self.connect() as connection:
            connection.execute("DELETE FROM fact_leases WHERE key=? AND owner=?", (self.key(key), owner))
