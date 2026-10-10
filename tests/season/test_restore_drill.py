"""A restore drill from a week-old backup reproduces the identical
leaderboard. Two simulated weeks of real worker jobs and bettors, a backup, one more
week of play, then the backup is staged and applied through the same path as the
container entrypoint (worker acknowledges, apply, migrate)."""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine, select

from app.core.clock import SystemClock
from app.core.migrations import upgrade_to_head
from app.models import Account, LedgerEntry
from app.services import backups, leaderboard, ledger, maintenance, sim
from tests.season.harness import SeasonRun, run_season

pytestmark = pytest.mark.season


@dataclass
class Drill:
    run: SeasonRun
    at_backup: list[tuple[int, str, int, int, int, int]]
    entries_at_backup: int
    after_week: list[tuple[int, str, int, int, int, int]]
    restored: list[tuple[int, str, int, int, int, int]]
    entries_restored: int
    applied: str
    backup_name: str


def board(engine: Engine) -> list[tuple[int, str, int, int, int, int]]:
    with engine.connect() as conn:
        season = ledger.active_season_id(conn)
        balances = dict(
            conn.execute(
                select(Account.user_id, Account.balance_cents).where(Account.season_id == season)
            ).all()
        )
        return [
            (s.user_id, s.display_name, s.pnl_cents, s.wins, s.open_bets, balances[s.user_id])
            for s in leaderboard.standings(conn, season)
        ]


def entries(engine: Engine) -> int:
    with engine.connect() as conn:
        return len(conn.execute(select(LedgerEntry.id)).all())


@pytest.fixture(scope="module")
def drill(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Drill]:
    run = run_season(tmp_path_factory.mktemp("restore-drill"), days=14)
    engine, settings = run.engine, run.settings
    path = backups.create(engine, settings, SystemClock(), label="week-old")
    at_backup, entries_at_backup = board(engine), entries(engine)
    sim.advance(engine, settings, timedelta(days=7))  # a week of drops, settlements, allowances
    after_week = board(engine)
    maintenance.stage_restore(engine, settings, SystemClock(), None, path)
    assert maintenance.worker_should_stop(settings)
    engine.dispose()
    applied = maintenance.apply(settings, wait_seconds=5, poll=0.05)
    upgrade_to_head(engine, Path(settings.data_dir) / ".migrate.lock")
    yield Drill(
        run,
        at_backup,
        entries_at_backup,
        after_week,
        board(engine),
        entries(engine),
        applied.status,
        path.name,
    )
    engine.dispose()


def test_restore_reproduces_the_leaderboard(drill: Drill) -> None:
    print(f"\nrestore drill: backup {drill.backup_name}; apply -> {drill.applied}")
    for label, rows in (
        ("at backup", drill.at_backup),
        ("+7 days", drill.after_week),
        ("restored", drill.restored),
    ):
        print(
            f"{label:>10}: "
            + ", ".join(f"{n} {p / 100:+.2f}/{b / 100:.2f}" for _, n, p, _, _, b in rows)
        )
    assert drill.applied == "restored"
    assert drill.after_week != drill.at_backup  # the extra week really changed things
    assert drill.restored == drill.at_backup
    assert drill.entries_restored == drill.entries_at_backup


def test_restored_ledger_verifies(drill: Drill) -> None:
    with drill.run.engine.connect() as conn:
        report = ledger.verify(conn)
    print(f"ledger: {report.accounts_checked} accounts, {report.txns_checked} txns, ok={report.ok}")
    assert report.ok
