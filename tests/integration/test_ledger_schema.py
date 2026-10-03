"""Database backstops for the ledger rules, independent of the service layer."""

import pytest
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import IntegrityError

from app.core.db import immediate

NOW = "2026-10-05 00:00:00.000000"  # SQLAlchemy's SQLite DateTime storage format


def _seed(conn: Connection) -> None:
    conn.execute(
        text("INSERT INTO seasons (id, number, status, started_at) VALUES (1, 1, 'active', :t)"),
        {"t": NOW},
    )
    conn.execute(
        text(
            "INSERT INTO users (id, email, display_name, role, status, created_at) "
            "VALUES (1, 'p@example.invalid', 'P', 'player', 'active', :t)"
        ),
        {"t": NOW},
    )
    conn.execute(
        text(
            "INSERT INTO accounts (id, kind, user_id, season_id, balance_cents, pnl_cents, "
            "created_at) VALUES (1, 'mint', NULL, 1, 0, 0, :t), (2, 'player', 1, 1, 0, 0, :t)"
        ),
        {"t": NOW},
    )
    conn.execute(
        text(
            "INSERT INTO ledger_txns (id, kind, created_at, idempotency_key, fingerprint) "
            "VALUES (1, 'allowance', :t, 'k1', 'f')"
        ),
        {"t": NOW},
    )
    conn.execute(
        text(
            "INSERT INTO ledger_entries (txn_id, account_id, amount_cents, kind) "
            "VALUES (1, 1, -500, 'allowance'), (1, 2, 500, 'allowance')"
        )
    )


@pytest.fixture
def seeded(migrated_engine: Engine) -> Engine:
    with immediate(migrated_engine) as conn:
        _seed(conn)
    return migrated_engine


def test_append_only_triggers_exist_at_head(migrated_engine: Engine) -> None:
    with migrated_engine.connect() as conn:
        triggers = set(
            conn.execute(text("SELECT name FROM sqlite_master WHERE type = 'trigger'")).scalars()
        )
    assert {
        "trg_ledger_entries_no_update",
        "trg_ledger_entries_no_delete",
        "trg_ledger_txns_no_update",
        "trg_ledger_txns_no_delete",
        "trg_observations_no_update",
        "trg_observations_no_delete",
    } <= triggers


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE ledger_entries SET amount_cents = 1 WHERE txn_id = 1",
        "DELETE FROM ledger_entries",
        "UPDATE ledger_txns SET memo = 'edited'",
        "DELETE FROM ledger_txns",
    ],
)
def test_ledger_rows_cannot_be_updated_or_deleted(seeded: Engine, statement: str) -> None:
    with pytest.raises(IntegrityError, match="append-only"), immediate(seeded) as conn:
        conn.execute(text(statement))


def test_player_balance_cannot_go_negative_in_the_database(seeded: Engine) -> None:
    with pytest.raises(IntegrityError, match="player_not_negative"), immediate(seeded) as conn:
        conn.execute(text("UPDATE accounts SET balance_cents = -1 WHERE id = 2"))
    with immediate(seeded) as conn:  # system accounts may go negative
        conn.execute(text("UPDATE accounts SET balance_cents = -500 WHERE id = 1"))


def test_only_one_open_season(seeded: Engine) -> None:
    with pytest.raises(IntegrityError, match="UNIQUE"), immediate(seeded) as conn:
        conn.execute(
            text("INSERT INTO seasons (number, status, started_at) VALUES (2, 'active', :t)"),
            {"t": NOW},
        )
    with immediate(seeded) as conn:
        conn.execute(text("UPDATE seasons SET ended_at = :t, status = 'ended'"), {"t": NOW})
        conn.execute(
            text("INSERT INTO seasons (number, status, started_at) VALUES (2, 'active', :t)"),
            {"t": NOW},
        )


def test_one_system_account_per_kind_per_season(seeded: Engine) -> None:
    with pytest.raises(IntegrityError, match="UNIQUE"), immediate(seeded) as conn:
        conn.execute(
            text(
                "INSERT INTO accounts (kind, season_id, balance_cents, pnl_cents, created_at) "
                "VALUES ('mint', 1, 0, 0, :t)"
            ),
            {"t": NOW},
        )


def test_player_account_requires_user(seeded: Engine) -> None:
    with pytest.raises(IntegrityError, match="player_has_user"), immediate(seeded) as conn:
        conn.execute(
            text(
                "INSERT INTO accounts (kind, season_id, balance_cents, pnl_cents, created_at) "
                "VALUES ('player', 1, 0, 0, :t)"
            ),
            {"t": NOW},
        )


def test_zero_entries_rejected(seeded: Engine) -> None:
    with pytest.raises(IntegrityError, match="amount_not_zero"), immediate(seeded) as conn:
        conn.execute(
            text(
                "INSERT INTO ledger_entries (txn_id, account_id, amount_cents, kind) "
                "VALUES (1, 1, 0, 'allowance')"
            )
        )
