"""PostgreSQL pool construction and explicit schema migration support."""

from __future__ import annotations

from pathlib import Path

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from wren_chat_api.config import Settings

MIGRATION_LOCK_KEY = 6_296_137_541_702_683_476


def default_migrations_dir() -> Path:
    """Locate migrations in an installed wheel or the source checkout."""
    package_migrations = Path(__file__).parent / "migrations"
    if package_migrations.is_dir():
        return package_migrations
    return Path(__file__).parents[2] / "migrations"


def create_app_pool(settings: Settings) -> AsyncConnectionPool:
    """Create the transactional pool used by audit and lease repositories."""
    return AsyncConnectionPool(
        conninfo=settings.state_database_url.get_secret_value(),
        kwargs={"row_factory": dict_row},
        open=False,
        name="wren-chat-app",
    )


def create_checkpoint_pool(settings: Settings) -> AsyncConnectionPool:
    """Create the isolated pool required by the LangGraph checkpointer."""
    return AsyncConnectionPool(
        conninfo=settings.state_database_url.get_secret_value(),
        kwargs={
            "autocommit": True,
            "prepare_threshold": 0,
            "row_factory": dict_row,
        },
        open=False,
        name="wren-chat-checkpoint",
    )


async def apply_migrations(
    pool: AsyncConnectionPool,
    migrations_dir: Path,
) -> None:
    """Apply each SQL migration once while holding a transaction-scoped lock."""
    if not migrations_dir.is_dir():
        raise FileNotFoundError(f"Migration directory does not exist: {migrations_dir}")

    migration_files = sorted(migrations_dir.glob("*.sql"))
    if not migration_files:
        raise RuntimeError(f"No SQL migrations found in: {migrations_dir}")

    async with pool.connection() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(%s)",
                (MIGRATION_LOCK_KEY,),
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS wren_chat_schema_migrations (
                    version TEXT PRIMARY KEY,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )

            cursor = await conn.execute(
                "SELECT version FROM wren_chat_schema_migrations"
            )
            applied_versions = {row["version"] for row in await cursor.fetchall()}

            for migration_file in migration_files:
                version = migration_file.stem
                if version in applied_versions:
                    continue

                await conn.execute(migration_file.read_text(encoding="utf-8"))
                await conn.execute(
                    """
                    INSERT INTO wren_chat_schema_migrations (version)
                    VALUES (%s)
                    """,
                    (version,),
                )
