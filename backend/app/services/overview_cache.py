"""公开只读概览的有界缓存；只缓存供应商事实，不缓存账户或投资建议。"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from threading import RLock
from time import monotonic
from weakref import WeakKeyDictionary

from backend.app.models.schemas import DataOverviewSection
from backend.app.services.market_overview import load_section, overview_jobs, overview_response


@dataclass
class Entry:
    provider: object
    sections: dict = field(default_factory=dict)
    expires: dict = field(default_factory=dict)
    tasks: dict = field(default_factory=dict)
    last_refresh: float = 0


class OverviewCache:
    def __init__(self, max_entries=64, max_pending=32, concurrency=8):
        self.max_entries, self.max_pending, self.concurrency = max_entries, max_pending, concurrency
        self.entries = OrderedDict()
        self.lock = RLock()
        self.semaphores = WeakKeyDictionary()

    @staticmethod
    def ttl(direction, section):
        if section.status != "ok":
            return 30
        if section.key == "macro":
            return 1800
        if section.key in {"detail", "company"}:
            return 900
        if section.key in {"news", "history"} or direction == "fund":
            return 300
        return 60

    async def _load(self, entry, direction, job, semaphore):
        key = job[0]
        try:
            async with asyncio.timeout(28):
                async with semaphore:
                    section = await load_section(entry.provider, *job)
            stamp = datetime.now(timezone.utc)
            ttl = self.ttl(direction, section)
            section.fetched_at, section.expires_at = stamp, stamp + timedelta(seconds=ttl)
            with self.lock:
                entry.sections[key], entry.expires[key] = section, monotonic() + ttl
        except asyncio.CancelledError:
            with self.lock:
                entry.expires.pop(key, None)
            raise
        except Exception:
            # 未预期的供应商失败不能留下永久 loading 或暴露内部异常。
            with self.lock:
                entry.sections[key] = DataOverviewSection(key=key, title=job[1], status="unavailable", message="本栏数据暂未取得，请稍后刷新。")
                entry.expires[key] = monotonic() + 30
        finally:
            with self.lock:
                entry.tasks.pop(key, None)

    async def get(self, provider, request, *, jobs=None, namespace="overview"):
        loop, now = asyncio.get_running_loop(), monotonic()
        key = (namespace, id(provider), request.direction, request.target or ("600519" if request.direction == "stock" else None))
        jobs = jobs if jobs is not None else overview_jobs(provider, request)
        with self.lock:
            entry = self.entries.get(key)
            if entry is None:
                # 不淘汰正在取数的键；也不允许任意目标撑大任务队列。
                while len(self.entries) >= self.max_entries:
                    victim = next((k for k, value in self.entries.items() if not value.tasks), None)
                    if victim is None:
                        return overview_response(request, [DataOverviewSection(key=j[0], title=j[1], status="unavailable", message="数据服务繁忙，请稍后刷新。") for j in jobs])
                    self.entries.pop(victim)
                entry = self.entries[key] = Entry(provider)
            self.entries.move_to_end(key)
            semaphore = self.semaphores.setdefault(loop, asyncio.Semaphore(self.concurrency))
            force = request.refresh and now - entry.last_refresh >= 10
            if force:
                entry.last_refresh = now
            for job in jobs:
                section_key = job[0]
                task = entry.tasks.get(section_key)
                if task and (task.done() or task.get_loop() is not loop):
                    entry.tasks.pop(section_key, None)
                    task = None
                if task or (not force and entry.expires.get(section_key, 0) > now):
                    continue
                pending = sum(len(value.tasks) for value in self.entries.values())
                if pending >= self.max_pending:
                    entry.sections[section_key] = DataOverviewSection(key=section_key, title=job[1], status="unavailable", message="数据服务繁忙，请稍后刷新。")
                    entry.expires[section_key] = now + 5
                    continue
                entry.sections[section_key] = DataOverviewSection(key=section_key, title=job[1], status="loading", message="正在更新本栏数据…")
                entry.tasks[section_key] = loop.create_task(self._load(entry, request.direction, job, semaphore))
            tasks = list(entry.tasks.values())
        if request.wait and tasks:
            # 浏览器断开只取消等待者，其他用户仍可复用同一个公共取数任务。
            await asyncio.gather(*(asyncio.shield(task) for task in tasks))
        with self.lock:
            sections = [entry.sections[job[0]].model_copy(deep=True) for job in jobs]
        return overview_response(request, sections)


    async def aclose(self):
        with self.lock:
            tasks = [task for entry in self.entries.values() for task in entry.tasks.values()]
            self.entries.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
