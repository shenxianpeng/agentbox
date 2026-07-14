"""Simple migration runner: applies migrations/*.sql in order."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import asyncpg

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent.parent.parent / "migrations"


async def ensure_schema_migrations_table(pool: asyncpg.Pool) -> None:
    """Create the schema_migrations tracking table if it doesn't exist."""
    async with pool.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version TEXT PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )


async def get_applied_migrations(pool: asyncpg.Pool) -> set[str]:
    """Return the set of already-applied migration filenames."""
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT version FROM schema_migrations ORDER BY version")
        return {r["version"] for r in rows}


async def apply_migration(pool: asyncpg.Pool, path: Path) -> None:
    """Apply a single migration file within a transaction."""
    sql = path.read_text()
    version = path.name
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Execute all statements in the file
            # Split on semicolon-newline to handle multi-statement files
            statements = re.split(r";\s*\n", sql)
            for stmt in statements:
                stmt = stmt.strip()
                if stmt:
                    await conn.execute(stmt)
            await conn.execute(
                "INSERT INTO schema_migrations (version) VALUES ($1)", version
            )
    print(f"  ✓ Applied {version}")


async def migrate(database_url: str | None = None) -> None:
    """Run all pending migrations."""
    from agentbox.settings import settings

    url = database_url or settings.database_url
    print(f"Migrating {url} ...")

    pool = await asyncpg.create_pool(url, min_size=1, max_size=1)
    try:
        await ensure_schema_migrations_table(pool)
        applied = await get_applied_migrations(pool)

        migration_files = sorted(MIGRATIONS_DIR.glob("*.sql"))
        pending = [f for f in migration_files if f.name not in applied]

        if not pending:
            print("  No pending migrations.")
            return

        for path in pending:
            await apply_migration(pool, path)

        print(f"  Done. Applied {len(pending)} migration(s).")
    finally:
        await pool.close()


def main() -> None:
    asyncio.run(migrate())


if __name__ == "__main__":
    main()
