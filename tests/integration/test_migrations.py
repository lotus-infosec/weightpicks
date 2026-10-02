from pathlib import Path

from sqlalchemy import Engine, inspect

from app.core.migrations import current_revision, head_revision, is_at_head, upgrade_to_head


def test_upgrade_creates_tables_and_is_idempotent(engine: Engine, tmp_path: Path) -> None:
    assert current_revision(engine) is None
    assert not is_at_head(engine)

    upgrade_to_head(engine, tmp_path / ".migrate.lock")
    upgrade_to_head(engine, tmp_path / ".migrate.lock")  # second run is a no-op

    assert current_revision(engine) == head_revision()
    assert is_at_head(engine)
    tables = set(inspect(engine).get_table_names())
    assert {"heartbeats", "job_runs", "accounts", "ledger_entries", "alembic_version"} <= tables
