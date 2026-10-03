"""STAGE06 measure: 60 simulated days of scripted betting keep every invariant."""

from collections import defaultdict
from collections.abc import Iterator

import pytest
from sqlalchemy import func, select

from app.cli import main
from app.domain.ledger import BETTING_KINDS
from app.models import (
    Account,
    Bet,
    BetLeg,
    LedgerEntry,
    Market,
    OutboxMessage,
    Settlement,
)
from tests.season.harness import SeasonRun, run_season

pytestmark = pytest.mark.season

SYNC_SLACK_HOURS = 3  # syncs run every 2 h outside the weigh-in window


@pytest.fixture(scope="module")
def season(tmp_path_factory: pytest.TempPathFactory) -> Iterator[SeasonRun]:
    run = run_season(tmp_path_factory.mktemp("season"))
    yield run
    run.engine.dispose()


def test_bettors_actually_bet(season: SeasonRun) -> None:
    assert season.placed > 200
    with season.engine.connect() as conn:
        statuses = dict(conn.execute(select(Bet.status, func.count()).group_by(Bet.status)).all())
        settled = conn.execute(select(func.count()).select_from(Settlement)).scalar_one()
    print(
        f"\nseason v1: {season.placed} bets placed, refused {dict(season.rejected)}, "
        f"bets {statuses}, {settled} markets settled"
    )
    for reason in ("insufficient_funds", "below_minimum", "stale_odds", "locked", "user_inactive"):
        assert season.expected_rejections[reason] > 0, reason


def test_ledger_verifies_and_money_is_conserved(
    season: SeasonRun, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from app.services import ledger

    with season.engine.connect() as conn:
        assert ledger.verify(conn).ok
        assert conn.execute(select(func.sum(Account.balance_cents))).scalar_one() == 0
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("DATA_DIR", str(season.settings.data_dir))
    assert main(["ledger", "verify"]) == 0
    assert "ledger OK" in capsys.readouterr().out


def test_pnl_counts_only_betting_entries(season: SeasonRun) -> None:
    with season.engine.connect() as conn:
        accounts = conn.execute(select(Account.id, Account.kind, Account.pnl_cents)).all()
        entries = conn.execute(
            select(LedgerEntry.account_id, LedgerEntry.kind, LedgerEntry.amount_cents)
        ).all()
    betting: dict[int, int] = defaultdict(int)
    for account_id, kind, amount in entries:
        if kind in BETTING_KINDS:
            betting[account_id] += amount
    players = [a for a in accounts if a.kind == "player"]
    house = next(a for a in accounts if a.kind == "house")
    for a in players:
        assert a.pnl_cents == betting[a.id]
    assert house.pnl_cents == -sum(a.pnl_cents for a in players)
    assert next(a for a in accounts if a.kind == "mint").pnl_cents == 0  # grants aren't P&L


def test_every_due_market_settled_once(season: SeasonRun) -> None:
    from datetime import timedelta

    cutoff = season.end - timedelta(hours=SYNC_SLACK_HOURS)
    with season.engine.connect() as conn:
        rows = conn.execute(
            select(Market.id, Market.status, Market.lock_at, Market.settle_after)
        ).all()
        settled_ids = list(conn.execute(select(Settlement.market_id)).scalars())
    assert len(settled_ids) == len(set(settled_ids))
    for r in rows:
        if r.settle_after <= cutoff:
            assert r.status == "settled", r
        elif r.lock_at <= season.end:
            assert r.status in ("locked", "settled"), r
        else:
            assert r.status == "open", r
    assert set(settled_ids) == {r.id for r in rows if r.status == "settled"}
    assert len(settled_ids) > 300


def test_every_bet_is_resolved_correctly_or_still_open(season: SeasonRun) -> None:
    with season.engine.connect() as conn:
        rows = conn.execute(
            select(
                Bet.id,
                Bet.status,
                Bet.stake_cents,
                Bet.potential_payout_cents,
                Bet.payout_cents,
                Bet.placed_at,
                BetLeg.status.label("leg_status"),
                Market.status.label("market_status"),
                Market.lock_at,
            )
            .join(BetLeg, BetLeg.bet_id == Bet.id)
            .join(Market, Market.id == BetLeg.market_id)
        ).all()
        results = conn.execute(
            select(func.count())
            .select_from(OutboxMessage)
            .where(OutboxMessage.category == "bet_results")
        ).scalar_one()
    resolved = 0
    for r in rows:
        assert r.placed_at < r.lock_at
        assert r.status == r.leg_status
        if r.market_status == "settled":
            resolved += 1
            expected = {"won": r.potential_payout_cents, "push": r.stake_cents, "lost": 0}
            assert r.payout_cents == expected[r.status], r
        else:
            assert r.status == "open" and r.payout_cents is None, r
    assert results == resolved > 100
    assert {r.status for r in rows} >= {"won", "lost"}
