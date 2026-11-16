"""Coalesce dependency probes, including when a blocking driver is slow."""
import asyncio
from time import monotonic


class DatabaseHealthProbe:
    def __init__(self, ttl=2, timeout=2):
        self.ttl, self.timeout = ttl, timeout
        self.task = None
        self.expires = 0

    async def check(self, database):
        if not database.configured or database.initialization_error:
            return False
        if self.task is None or (self.task.done() and monotonic() >= self.expires):
            self.task = asyncio.create_task(asyncio.to_thread(self._ping, database))
            self.expires = monotonic() + self.ttl
        try:
            return await asyncio.wait_for(asyncio.shield(self.task), self.timeout)
        except Exception:
            return False

    @staticmethod
    def _ping(database):
        try:
            from sqlalchemy import text
            with database.engine.connect() as connection:
                return connection.scalar(text("SELECT 1")) == 1
        except Exception:
            return False
