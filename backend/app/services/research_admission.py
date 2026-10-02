"""Bound active research work; reject overload before starting paid calls."""
import asyncio
import os


class ResearchAdmission:
    def __init__(self, limit=None, queue_seconds=None):
        self.limit = int(limit if limit is not None else os.getenv("WENCE_RESEARCH_MAX_CONCURRENCY", "8"))
        self.queue_seconds = float(queue_seconds if queue_seconds is not None else os.getenv("WENCE_RESEARCH_QUEUE_SECONDS", "1"))
        if self.limit < 1 or not 0 < self.queue_seconds <= 3:
            raise ValueError("研究并发上限必须为正，排队预算须在 0 至 3 秒之间")
        self.loop = None
        self.semaphore = None

    async def acquire(self):
        loop = asyncio.get_running_loop()
        if loop is not self.loop:
            self.loop = loop
            self.semaphore = asyncio.Semaphore(self.limit)
        semaphore = self.semaphore
        await asyncio.wait_for(semaphore.acquire(), timeout=self.queue_seconds)
        return semaphore
