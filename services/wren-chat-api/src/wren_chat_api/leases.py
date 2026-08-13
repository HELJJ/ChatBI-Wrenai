"""Atomic PostgreSQL leases that serialize requests for one session."""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID, uuid4

from psycopg_pool import AsyncConnectionPool

from wren_chat_api.contracts import SessionId, StrictModel


class Lease(StrictModel):
    """Ownership token for one live session request."""

    session_id: SessionId
    lease_id: UUID
    expires_at: datetime


class LeaseRepository:
    """Acquire, renew, and release expiring session ownership tokens."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self.pool = pool

    async def acquire(self, session_id: str, *, ttl: timedelta) -> Lease | None:
        """Acquire a missing or expired lease atomically."""
        _validate_ttl(ttl)
        lease_id = uuid4()
        async with self.pool.connection() as conn:
            cursor = await conn.execute(
                """
                INSERT INTO chat_session_leases (
                    session_id, lease_id, expires_at, updated_at
                )
                VALUES (
                    %s, %s, clock_timestamp() + %s, clock_timestamp()
                )
                ON CONFLICT (session_id) DO UPDATE
                SET lease_id = EXCLUDED.lease_id,
                    expires_at = EXCLUDED.expires_at,
                    updated_at = EXCLUDED.updated_at
                WHERE chat_session_leases.expires_at <= clock_timestamp()
                RETURNING session_id, lease_id, expires_at
                """,
                (session_id, lease_id, ttl),
            )
            row = await cursor.fetchone()

        return None if row is None else Lease.model_validate(row)

    async def renew(self, lease: Lease, *, ttl: timedelta) -> bool:
        """Renew only the still-live lease owned by the supplied token."""
        _validate_ttl(ttl)
        async with self.pool.connection() as conn:
            cursor = await conn.execute(
                """
                UPDATE chat_session_leases
                SET expires_at = clock_timestamp() + %s,
                    updated_at = clock_timestamp()
                WHERE session_id = %s
                  AND lease_id = %s
                  AND expires_at > clock_timestamp()
                """,
                (ttl, lease.session_id, lease.lease_id),
            )
        return cursor.rowcount == 1

    async def release(self, lease: Lease) -> bool:
        """Release only the lease owned by the supplied token."""
        async with self.pool.connection() as conn:
            cursor = await conn.execute(
                """
                DELETE FROM chat_session_leases
                WHERE session_id = %s
                  AND lease_id = %s
                """,
                (lease.session_id, lease.lease_id),
            )
        return cursor.rowcount == 1


def _validate_ttl(ttl: timedelta) -> None:
    if ttl <= timedelta(0):
        raise ValueError("ttl must be positive")
