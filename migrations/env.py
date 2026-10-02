"""Alembic environment: URL from app settings, batch mode for SQLite ALTERs."""

from alembic import context
from sqlalchemy import Connection

from app.core.config import Settings
from app.core.db import make_engine
from app.models import Base

target_metadata = Base.metadata


def _run(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # `wp migrate` passes an open connection; the alembic CLI builds one from settings.
    connection = context.config.attributes.get("connection")
    if connection is not None:
        _run(connection)
        return
    engine = make_engine(Settings().db_url)
    try:
        with engine.connect() as conn:
            _run(conn)
    finally:
        engine.dispose()


if context.is_offline_mode():
    raise SystemExit("offline (--sql) migrations are not supported")
run_migrations_online()
