import asyncio
import logging
from typing import Callable, Coroutine

log = logging.getLogger("epicvibe.jobs")


class JobRunner:
    def __init__(self):
        self._tasks: dict[str, asyncio.Task] = {}

    def enqueue(self, key: str, coro_factory: Callable[[], Coroutine]) -> bool:
        if key in self._tasks and not self._tasks[key].done():
            return False
        task = asyncio.create_task(self._run(key, coro_factory))
        self._tasks[key] = task
        return True

    async def _run(self, key: str, coro_factory):
        try:
            await coro_factory()
        except Exception:
            log.exception("background job failed (key=%s)", key)
        finally:
            self._tasks.pop(key, None)

    async def join(self) -> None:
        await asyncio.gather(*list(self._tasks.values()), return_exceptions=True)
