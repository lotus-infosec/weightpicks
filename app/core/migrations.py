"""Run and inspect Alembic migrations from app code (`wp migrate`, worker startup)."""

import fcntl
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, Engine

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


def _config(connection: Connection | None = None) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    if connection is not None:
        cfg.attributes["connection"] = connection
    return cfg


def head_revision() -> str | None:
    return ScriptDirectory.from_config(_config()).get_current_head()


def known_revisions() -> set[str]:
    """Every revision this code knows; a backup from a newer schema isn't among them."""
    return {r.revision for r in ScriptDirectory.from_config(_config()).walk_revisions()}


def current_revision(engine: Engine) -> str | None:
    with engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


def is_at_head(engine: Engine) -> bool:
    return current_revision(engine) == head_revision()


@contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def upgrade_to_head(engine: Engine, lock_path: Path) -> None:
    """Upgrade under an exclusive file lock so two processes never migrate at once."""
    with _file_lock(lock_path), engine.begin() as conn:
        command.upgrade(_config(conn), "head")
