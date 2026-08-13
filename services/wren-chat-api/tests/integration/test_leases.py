"""PostgreSQL integration coverage for session lease ownership."""

import asyncio
from datetime import timedelta


async def test_only_one_concurrent_live_lease_can_be_acquired(leases) -> None:
    first, second = await asyncio.gather(
        leases.acquire("same-session", ttl=timedelta(seconds=30)),
        leases.acquire("same-session", ttl=timedelta(seconds=30)),
    )

    assert sum(lease is not None for lease in (first, second)) == 1


async def test_different_sessions_can_hold_leases_concurrently(leases) -> None:
    first, second = await asyncio.gather(
        leases.acquire("session-a", ttl=timedelta(seconds=30)),
        leases.acquire("session-b", ttl=timedelta(seconds=30)),
    )

    assert first is not None
    assert second is not None
    assert first.session_id == "session-a"
    assert second.session_id == "session-b"


async def test_expired_lease_can_be_replaced(app_pool, leases) -> None:
    first = await leases.acquire("replace-session", ttl=timedelta(seconds=30))
    assert first is not None
    async with app_pool.connection() as conn:
        await conn.execute(
            """
            UPDATE chat_session_leases
            SET expires_at = clock_timestamp() - interval '1 second'
            WHERE session_id = %s
            """,
            (first.session_id,),
        )

    second = await leases.acquire("replace-session", ttl=timedelta(seconds=30))

    assert second is not None
    assert second.lease_id != first.lease_id


async def test_expired_holder_cannot_renew_or_release_replacement(
    app_pool, leases
) -> None:
    first = await leases.acquire("owned-session", ttl=timedelta(seconds=30))
    assert first is not None
    async with app_pool.connection() as conn:
        await conn.execute(
            """
            UPDATE chat_session_leases
            SET expires_at = clock_timestamp() - interval '1 second'
            WHERE session_id = %s
            """,
            (first.session_id,),
        )
    second = await leases.acquire("owned-session", ttl=timedelta(seconds=30))
    assert second is not None

    assert await leases.renew(first, ttl=timedelta(seconds=30)) is False
    assert await leases.release(first) is False
    assert await leases.renew(second, ttl=timedelta(seconds=30)) is True
    assert await leases.release(second) is True
