"""60 simulated days of scripted betting keep every invariant."""

from collections import defaultdict
from collections.abc import Iterator
from datetime import timedelta

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
    Pool,
    PoolEntry,
    Settlement,
)
from tests.season.harness import START as SEASON_START
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


def test_busts_and_bailouts_are_consistent(season: SeasonRun) -> None:
    from app.models import Bust, LedgerTxn

    with season.engine.connect() as conn:
        rows = conn.execute(select(Bust.busted_at, Bust.bailed_out_at, Bust.resolved_at)).all()
        bailout_txns = conn.execute(
            select(func.count()).select_from(LedgerTxn).where(LedgerTxn.kind == "bailout")
        ).scalar_one()
    assert len(rows) >= 1, "the reckless bettor should go bust at least once"
    assert season.bailouts >= 1
    assert bailout_txns == season.bailouts == sum(1 for r in rows if r.bailed_out_at)
    for busted_at, bailed, resolved in rows:
        assert not (bailed and resolved)
        if bailed:
            assert bailed - busted_at >= timedelta(days=2)  # cooldown honoured
    # P&L excludes bailouts: covered by test_pnl_counts_only_betting_entries.


def test_leaderboard_equals_ledger_pnl(season: SeasonRun) -> None:
    """The leaderboard ranks by settled P&L: betting entries plus open stakes and buy-ins."""
    from app.services import leaderboard
    from app.services.ledger import active_season_id

    with season.engine.connect() as conn:
        season_id = active_season_id(conn)
        rows = leaderboard.standings(conn, season_id)
        derived = dict(
            conn.execute(
                select(Account.user_id, func.coalesce(func.sum(LedgerEntry.amount_cents), 0))
                .outerjoin(
                    LedgerEntry,
                    (LedgerEntry.account_id == Account.id)
                    & LedgerEntry.kind.in_([k.value for k in BETTING_KINDS]),
                )
                .where(Account.season_id == season_id, Account.user_id.is_not(None))
                .group_by(Account.user_id)
            ).all()
        )
        # Settled P&L = the ledger's betting P&L plus what is still riding (issue #42).
        open_stakes = dict(
            conn.execute(
                select(Bet.user_id, func.sum(Bet.stake_cents))
                .where(Bet.season_id == season_id, Bet.status == "open")
                .group_by(Bet.user_id)
            ).all()
        )
        open_buyins = dict(
            conn.execute(
                select(PoolEntry.user_id, func.sum(Pool.buy_in_cents))
                .join(Pool, Pool.id == PoolEntry.pool_id)
                .where(Pool.season_id == season_id, Pool.status.in_(("open", "locked")))
                .group_by(PoolEntry.user_id)
            ).all()
        )
    assert len(rows) == len(season.players)
    assert {r.user_id: r.pnl_cents for r in rows} == {
        u: derived[u] + open_stakes.get(u, 0) + open_buyins.get(u, 0) for u in season.players
    }
    assert [r.pnl_cents for r in rows] == sorted((r.pnl_cents for r in rows), reverse=True)
    assert [r.rank for r in rows] == list(range(1, len(rows) + 1))
    assert any(r.pnl_cents != 0 for r in rows)


def test_daily_allowances_paid_and_kept_out_of_pnl(season: SeasonRun) -> None:
    """One allowance per player per day from the day after joining."""
    from app.domain.ledger import EntryKind

    with season.engine.connect() as conn:
        per_user = dict(
            conn.execute(
                select(Account.user_id, func.count())
                .join(LedgerEntry, LedgerEntry.account_id == Account.id)
                .where(LedgerEntry.kind == EntryKind.ALLOWANCE.value, Account.user_id.is_not(None))
                .group_by(Account.user_id)
            ).all()
        )
    days = (season.end - timedelta(hours=1)).date() - SEASON_START
    assert set(per_user) == set(season.players)
    assert all(abs(n - days.days) <= 1 for n in per_user.values()), per_user
