from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine, func, select, text

from app.cli import main
from app.core.clock import SimClock
from app.core.db import immediate, make_engine
from app.models import Account, JobRun, User
from app.worker.jobs import LedgerVerifyJob
from app.worker.registry import run_due


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LOG_FORMAT", "console")
    assert main(["migrate"]) == 0
    return tmp_path


def _engine(data_dir: Path) -> Engine:
    return make_engine(f"sqlite:///{data_dir / 'app.db'}")


def test_seed_creates_players_with_starting_bankroll_and_is_repeatable(
    cli_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["seed", "--users", "10"]) == 0
    assert main(["seed", "--users", "10"]) == 0  # same keys: no double grant
    assert "seeded 10 players in season 1, $1,000.00 each" in capsys.readouterr().out
    engine = _engine(cli_env)
    with engine.connect() as conn:
        emails = conn.execute(select(User.email).order_by(User.id)).scalars().all()
        balances = conn.execute(
            select(Account.balance_cents).where(Account.kind == "player")
        ).scalars()
        assert set(balances) == {100_000}
        mint = conn.execute(select(Account.balance_cents).where(Account.kind == "mint")).scalar()
    engine.dispose()
    assert len(emails) == 10
    assert all(e.endswith("@example.invalid") for e in emails)
    assert mint == -1_000_000


def test_seed_refuses_outside_dev(cli_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("APP_SECRET_KEY", "x" * 40)
    assert main(["seed"]) == 2


def test_ledger_verify_exit_codes(cli_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    main(["seed", "--users", "3"])
    assert main(["ledger", "verify"]) == 0
    assert "ledger OK: 5 accounts, 3 txns" in capsys.readouterr().out

    engine = _engine(cli_env)
    with immediate(engine) as conn:
        conn.execute(text("UPDATE accounts SET balance_cents = balance_cents + 1 WHERE id = 3"))
    engine.dispose()
    assert main(["ledger", "verify"]) == 1
    out = capsys.readouterr().out
    assert "ledger MISMATCH" in out
    assert "account 3: balance stored 100001 computed 100000" in out


@pytest.mark.parametrize(
    ("now", "key"),
    [
        (datetime(2026, 10, 5, 3, 0, tzinfo=UTC), "2026-10-05"),
        (datetime(2026, 10, 5, 23, 59, tzinfo=UTC), "2026-10-05"),
        (datetime(2026, 10, 6, 2, 59, tzinfo=UTC), "2026-10-05"),  # catch-up after downtime
    ],
)
def test_ledger_verify_job_period_key(now: datetime, key: str) -> None:
    assert LedgerVerifyJob().due(now) == key


def test_ledger_verify_job_records_ok_then_error(cli_env: Path) -> None:
    main(["seed", "--users", "2"])
    engine = _engine(cli_env)
    clock = SimClock(datetime(2026, 10, 5, 3, 1, tzinfo=UTC))
    run_due([LedgerVerifyJob()], engine, clock)
    with immediate(engine) as conn:
        conn.execute(text("UPDATE accounts SET pnl_cents = 7 WHERE kind = 'player'"))
    clock.advance(timedelta(days=1))
    run_due([LedgerVerifyJob()], engine, clock)
    with engine.connect() as conn:
        statuses = conn.execute(
            select(JobRun.period_key, JobRun.status, JobRun.error).order_by(JobRun.id)
        ).all()
        runs = conn.execute(select(func.count()).select_from(JobRun)).scalar_one()
    engine.dispose()
    assert runs == 2
    assert statuses[0][:2] == ("2026-10-05", "ok")
    assert statuses[1][:2] == ("2026-10-06", "error")
    assert "2 account mismatches" in (statuses[1][2] or "")
