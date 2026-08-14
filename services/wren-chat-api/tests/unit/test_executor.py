"""Unit contracts for bounded blocking-call admission."""

import asyncio
import threading
import time

import pytest

from wren_chat_api.errors import CapacityExceeded
from wren_chat_api.executor import BoundedWrenExecutor


async def test_submission_beyond_capacity_fails_immediately() -> None:
    executor = BoundedWrenExecutor(workers=1, queue_capacity=1)
    release = threading.Event()

    def blocked() -> str:
        release.wait(10)
        return "done"

    first = asyncio.create_task(executor.run(blocked))
    second = asyncio.create_task(executor.run(blocked))
    await asyncio.sleep(0.2)

    with pytest.raises(CapacityExceeded):
        await executor.run(blocked)

    release.set()
    assert await asyncio.gather(first, second) == ["done", "done"]
    executor.shutdown()


async def test_cancelled_await_keeps_slot_until_thread_completes() -> None:
    executor = BoundedWrenExecutor(workers=1, queue_capacity=0)
    started = threading.Event()
    release = threading.Event()

    def blocked() -> None:
        started.set()
        release.wait(10)

    task = asyncio.create_task(executor.run(blocked))
    while not started.is_set():
        await asyncio.sleep(0.01)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    with pytest.raises(CapacityExceeded):
        await executor.run(lambda: "x")

    release.set()
    admitted = False
    for _ in range(200):
        try:
            assert await executor.run(lambda: "x") == "x"
            admitted = True
            break
        except CapacityExceeded:
            await asyncio.sleep(0.02)
    assert admitted, "capacity was never released after the thread finished"
    executor.shutdown()


async def test_concurrency_never_exceeds_worker_count() -> None:
    executor = BoundedWrenExecutor(workers=2, queue_capacity=10)
    lock = threading.Lock()
    active = 0
    peak = 0

    def work() -> None:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.05)
        with lock:
            active -= 1

    await asyncio.gather(*[executor.run(work) for _ in range(6)])

    assert peak == 2
    executor.shutdown()


async def test_shutdown_stops_new_admission() -> None:
    executor = BoundedWrenExecutor(workers=1, queue_capacity=1)
    executor.shutdown()

    with pytest.raises(RuntimeError):
        await executor.run(lambda: "x")
