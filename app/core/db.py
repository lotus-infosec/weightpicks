"""SQLite engine with WAL pragmas and explicit `BEGIN IMMEDIATE` write transactions.

pysqlite's implicit transaction handling is switched off so SQLAlchemy controls
BEGIN itself (the SQLAlchemy "serializable isolation / savepoints" SQLite recipe).
Every write path uses `immediate()`: one short transaction that takes the write
lock up front, so it can never fail half-way with "database is locked".
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import Connection, Engine, create_engine, event

PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA foreign_keys=ON",
    "PRAGMA busy_timeout=5000",
)
_BEGIN_KEY = "wp_begin_sql"


def make_engine(url: str) -> Engine:
    engine = create_engine(url)

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_conn: Any, _record: Any) -> None:
        dbapi_conn.isolation_level = None
        cursor = dbapi_conn.cursor()
        for pragma in PRAGMAS:
            cursor.execute(pragma)
        cursor.close()

    @event.listens_for(engine, "begin")
    def _on_begin(conn: Connection) -> None:
        conn.exec_driver_sql(conn.info.pop(_BEGIN_KEY, "BEGIN"))

    return engine


@contextmanager
def immediate(engine: Engine) -> Iterator[Connection]:
    """A write transaction: BEGIN IMMEDIATE, commit on success, roll back on error."""
    with engine.connect() as conn:
        conn.info[_BEGIN_KEY] = "BEGIN IMMEDIATE"
        with conn.begin():
            yield conn
