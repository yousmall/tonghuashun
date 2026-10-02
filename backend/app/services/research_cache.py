"""Bounded, process-local public-source cache and duplicate-call coalescing."""
from __future__ import annotations

import asyncio
import time
import sqlite3
from collections import OrderedDict
from datetime import datetime, timedelta
from typing import Awaitable, Callable

from backend.app.fact_taxonomy import fact_is_current
from backend.app.models import FactRecord
from backend.app.services.public_fact_store import SqlitePublicFactStore
from backend.app.services.provider_errors import ProviderCallError


class ResearchFactCache:
    def __init__(self, max_entries: int = 128, max_pending: int = 32,
                 *, shared_path: str | None = None, lease_seconds: float = 35) -> None:
        self.max_entries, self.max_pending = max_entries, max_pending
        self.entries: OrderedDict[str, list[FactRecord]] = OrderedDict()
        self.pending: dict[str, asyncio.Task] = {}
        self.refreshing: set[str] = set()
        self.refreshed_at: dict[str, datetime] = {}
        self.loop = None
        self.shared = SqlitePublicFactStore(shared_path, max_entries=max_entries, lease_seconds=lease_seconds) if shared_path else None
        self.shared_error: str | None = None

    async def _shared_load(self, key, loader, now, *, force_refresh, cooldown):
        store = self.shared
        started = time.time()
        waited = False
        while True:
            row = await asyncio.to_thread(store.read, key)
            if row:
                facts, updated, refreshed = row
                valid = bool(facts) and all(fact_is_current(fact, now()) for fact in facts)
                repair_available = refreshed and (updated >= started if waited else
                    cooldown > 0 and 0 <= time.time() - updated < cooldown)
                if valid and (not force_refresh or repair_available):
                    return facts, True
            rejection = await asyncio.to_thread(store.permission_failure, key)
            if rejection:
                raise rejection
            owner = await asyncio.to_thread(store.claim, key)
            if owner:
                try:
                    # A publisher may have finished between read and claim.
                    row = await asyncio.to_thread(store.read, key)
                    if row and row[1] >= started and all(fact_is_current(f, now()) for f in row[0]) and (not force_refresh or row[2]):
                        return row[0], True
                    rejection = await asyncio.to_thread(store.permission_failure, key)
                    if rejection:
                        raise rejection
                    try:
                        facts = await loader()
                    except ProviderCallError as exc:
                        try:
                            await asyncio.to_thread(store.publish_permission_failure, key, owner, exc)
                        except sqlite3.Error as store_error:
                            self.shared_error = type(store_error).__name__
                        raise
                    if facts and all(fact_is_current(f, now()) for f in facts):
                        try:
                            await asyncio.to_thread(store.publish, key, owner, facts, force_refresh)
                        except sqlite3.Error as exc:
                            self.shared_error = type(exc).__name__
                    return facts, False
                finally:
                    try:
                        await asyncio.to_thread(store.release, key, owner)
                    except sqlite3.Error as exc:
                        self.shared_error = type(exc).__name__
            waited = True
            await asyncio.sleep(.025)

    async def aclose(self) -> None:
        tasks = list(self.pending.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.pending.clear()
        self.refreshing.clear()
        self.entries.clear()
        self.refreshed_at.clear()

    async def get(self, key: str, loader: Callable[[], Awaitable[list[FactRecord]]], now: Callable[[], datetime],
                  *, force_refresh: bool = False, refresh_cooldown_seconds: float = 0) -> tuple[list[FactRecord], bool]:
        loop = asyncio.get_running_loop()
        if self.loop is not loop:
            self.loop = loop
            self.pending = {}  # never await another event loop's task
            self.refreshing = set()
        cached = self.entries.get(key)
        current = now()
        valid = bool(cached) and all(fact_is_current(fact, current) for fact in cached)
        refreshed = self.refreshed_at.get(key)
        recent_refresh = (refresh_cooldown_seconds > 0 and refreshed is not None
                          and refreshed <= current < refreshed + timedelta(seconds=refresh_cooldown_seconds))
        if valid and (not force_refresh or recent_refresh):
            self.entries.move_to_end(key)
            return [fact.model_copy(deep=True) for fact in cached], True
        if not valid:
            self.entries.pop(key, None)
            self.refreshed_at.pop(key, None)
        task = self.pending.get(key)
        if force_refresh and task is not None and key not in self.refreshing:
            # An initial fetch cannot satisfy a forced repair. Finish it first
            # and then coalesce repair callers into a single fresh read.
            await asyncio.shield(task)
            return await self.get(key, loader, now, force_refresh=True,
                                  refresh_cooldown_seconds=refresh_cooldown_seconds)
        shared = task is not None
        if task is None:
            if len(self.pending) >= self.max_pending:
                return await loader(), False

            async def load() -> list[FactRecord]:
                try:
                    nonlocal shared
                    if self.shared:
                        try:
                            facts, shared = await self._shared_load(key, loader, now,
                                force_refresh=force_refresh, cooldown=refresh_cooldown_seconds)
                        except sqlite3.Error as exc:
                            self.shared_error = type(exc).__name__
                            facts = await loader()
                    else:
                        facts = await loader()
                    if facts and all(fact_is_current(fact, now()) for fact in facts):
                        self.entries[key] = [fact.model_copy(deep=True) for fact in facts]
                        self.entries.move_to_end(key)
                        if force_refresh:
                            self.refreshed_at[key] = now()
                        while len(self.entries) > self.max_entries:
                            expired_key, _ = self.entries.popitem(last=False)
                            self.refreshed_at.pop(expired_key, None)
                    return facts
                finally:
                    if self.pending.get(key) is asyncio.current_task():
                        self.pending.pop(key, None)
                        self.refreshing.discard(key)

            task = asyncio.create_task(load())
            # Retrieve errors even when the sole waiter was cancelled.
            task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
            self.pending[key] = task
            if force_refresh:
                self.refreshing.add(key)
        facts = await asyncio.shield(task)
        return [fact.model_copy(deep=True) for fact in facts], shared
