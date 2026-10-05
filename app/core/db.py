"""SQLite engine with WAL pragmas and explicit `BEGIN IMMEDIATE` write transactions.

pysqlite's implicit transaction handling is switched off so SQLAlchemy controls
BEGIN itself (the SQLAlchemy "serializable isolation / savepoints" SQLite recipe).
Every write path uses `immediate()`: one short transaction that takes the write
lock up front, so it can never fail half-way with "database is locked".

Within one process, write transactions also queue on a lock before BEGIN IMMEDIATE.
SQLite's busy handler polls with sleeps rather than queueing, so under many concurrent
writers (the STAGE16 load test: 50 bettors while settlement runs) an unlucky thread
could wait past busy_timeout. With the lock, threads wait their turn in Python and
busy_timeout only covers the other process (web vs worker).
"""

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import Connection, Engine, create_engine, event

PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA foreign_keys=ON",
    "PRAGMA busy_timeout=10000",
)
_BEGIN_KEY = "wp_begin_sql"
_WRITERS = threading.RLock()  # one write transaction at a time in this process


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
    with _WRITERS, engine.connect() as conn:
        conn.info[_BEGIN_KEY] = "BEGIN IMMEDIATE"
        with conn.begin():
            yield conn
