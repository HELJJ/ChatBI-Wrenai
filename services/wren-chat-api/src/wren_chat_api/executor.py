"""Bounded admission for synchronous Wren calls on a thread pool."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

from wren_chat_api.errors import CapacityExceeded


class BoundedWrenExecutor:
    """Run blocking Wren calls under a hard admission cap.

    At most ``workers + queue_capacity`` calls may be live or queued at any
    moment; excess submissions fail fast with ``CapacityExceeded`` instead of
    piling up behind a slow data source.
    """

    def __init__(self, *, workers: int, queue_capacity: int) -> None:
        if workers < 1:
            raise ValueError("workers must be positive")
        if queue_capacity < 0:
            raise ValueError("queue_capacity must be non-negative")
        self._executor = ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="wren-query",
        )
        self._admission = threading.Semaphore(workers + queue_capacity)
        self._closed = False
        self._lock = threading.Lock()

    async def run(self, func: Callable[..., Any], *args: Any) -> Any:
        """Submit one blocking call and await it without early slot release.

        The admission slot is reserved before ``submit()`` and returned only
        by the future's done callback, i.e. when the real thread work
        finishes. Awaiting through ``wrap_future`` plus ``shield`` means a
        cancelled coroutine never cancels the accounting or frees its slot
        while the underlying database call is still running.
        """
        with self._lock:
            if self._closed:
                raise RuntimeError("BoundedWrenExecutor is shut down")
        if not self._admission.acquire(blocking=False):
            raise CapacityExceeded("Wren executor is at capacity")

        try:
            future: Future = self._executor.submit(func, *args)
        except RuntimeError:
            self._admission.release()
            raise

        future.add_done_callback(lambda _future: self._admission.release())
        return await asyncio.shield(asyncio.wrap_future(future))

    def shutdown(self) -> None:
        """Stop admitting new calls and wait for active ones to finish."""
        with self._lock:
            self._closed = True
        self._executor.shutdown(wait=True)
