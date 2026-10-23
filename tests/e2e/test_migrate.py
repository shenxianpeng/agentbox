"""Migration runner tests against a real Postgres."""

from __future__ import annotations

import uuid

from agentbox.db import migrate as migrate_module
from agentbox.db.migrate import migrate
from agentbox.settings import settings


async def test_migrate_applies_pending_files_once(pool, tmp_path, monkeypatch, capsys):
    tag = uuid.uuid4().hex[:8]
    version = f"999_cov_{tag}.sql"
    (tmp_path / version).write_text(f"CREATE TABLE cov_probe_{tag} (id INT PRIMARY KEY);")
    monkeypatch.setattr(migrate_module, "MIGRATIONS_DIR", tmp_path)

    try:
        await migrate(settings.database_url)
        out = capsys.readouterr().out
        assert f"Migrating {settings.database_url} ..." in out
        assert f"✓ Applied {version}" in out
        assert "Done. Applied 1 migration(s)." in out

        async with pool.acquire() as conn:
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM schema_migrations WHERE version = $1", version
                )
                == 1
            )
            assert await conn.fetchval("SELECT to_regclass($1)", f"cov_probe_{tag}") is not None

        # Second run: nothing pending, nothing re-applied
        await migrate()
        assert "No pending migrations." in capsys.readouterr().out
    finally:
        async with pool.acquire() as conn:
            await conn.execute(f"DROP TABLE IF EXISTS cov_probe_{tag}")
            await conn.execute("DELETE FROM schema_migrations WHERE version = $1", version)


def test_main_runs_migrations_synchronously(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(migrate_module, "MIGRATIONS_DIR", tmp_path)  # nothing to apply

    migrate_module.main()

    out = capsys.readouterr().out
    assert "Migrating" in out
    assert "No pending migrations." in out
