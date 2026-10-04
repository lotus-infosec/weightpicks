"""STAGE12 measure: 60 simulated days with core markets, admin props and futures, and
parlays keep every invariant (season v2). The generic v1 invariants run here too, on this
season (imported tests use this module's `season` fixture)."""

from collections.abc import Iterator

import pytest
from sqlalchemy import func, select

from app.domain import parlay
from app.models import Bet, BetLeg, Market, Settlement
from tests.season.harness import SeasonRun, run_season
from tests.season.test_season_v1 import (  # noqa: F401 - re-run on the v2 season
    test_busts_and_bailouts_are_consistent,
    test_daily_allowances_paid_and_kept_out_of_pnl,
    test_every_due_market_settled_once,
    test_leaderboard_equals_ledger_pnl,
    test_ledger_verifies_and_money_is_conserved,
    test_pnl_counts_only_betting_entries,
)

pytestmark = pytest.mark.season


@pytest.fixture(scope="module")
def season(tmp_path_factory: pytest.TempPathFactory) -> Iterator[SeasonRun]:
    run = run_season(tmp_path_factory.mktemp("season-v2"), v2=True)
    yield run
    run.engine.dispose()


def test_props_and_parlays_happened(season: SeasonRun) -> None:
    with season.engine.connect() as conn:
        by_template = dict(
            conn.execute(
                select(Market.template, func.count())
                .where(Market.origin == "admin")
                .group_by(Market.template)
            ).all()
        )
        settled_props = conn.execute(
            select(func.count())
            .select_from(Settlement)
            .join(Market, Market.id == Settlement.market_id)
            .where(Market.origin == "admin")
        ).scalar_one()
        parlay_status = dict(
            conn.execute(
                select(Bet.status, func.count()).where(Bet.kind == "parlay").group_by(Bet.status)
            ).all()
        )
    print(
        f"\nseason v2: props {dict(season.props)} -> markets {by_template}, "
        f"{settled_props} settled; parlays {season.parlays} placed {parlay_status}; "
        f"refused {dict(season.rejected)}"
    )
    assert set(by_template) >= {"milestone_by", "streak_reaches", "beat_last_week"}
    assert settled_props >= 10
    assert season.parlays > 50
    assert {"won", "lost"} <= set(parlay_status)


def test_every_parlay_resolves_from_its_legs(season: SeasonRun) -> None:
    with season.engine.connect() as conn:
        bets = conn.execute(
            select(
                Bet.id, Bet.status, Bet.stake_cents, Bet.potential_payout_cents, Bet.payout_cents
            ).where(Bet.kind == "parlay")
        ).all()
        legs: dict[int, list[tuple[str, int, list[str], str, int]]] = {}
        for bet_id, status, american, keys, market_status, market_id in conn.execute(
            select(
                BetLeg.bet_id,
                BetLeg.status,
                BetLeg.american,
                Market.correlation_keys,
                Market.status,
                Market.id,
            )
            .join(Market, Market.id == BetLeg.market_id)
            .join(Bet, Bet.id == BetLeg.bet_id)
            .where(Bet.kind == "parlay")
        ):
            legs.setdefault(bet_id, []).append((status, american, keys, market_status, market_id))
    for b in bets:
        mine = legs[b.id]
        assert 2 <= len(mine) <= 6
        assert len({m for *_, m in mine}) == len(mine)  # never the same market twice
        assert parlay.correlated([k for _, _, k, _, _ in mine]) is None  # never correlated
        assert b.potential_payout_cents == parlay.potential_payout_cents(
            b.stake_cents, [a for _, a, *_ in mine]
        )
        assert b.potential_payout_cents <= parlay.PAYOUT_CAP_MULTIPLE * b.stake_cents
        expected = parlay.resolve(b.stake_cents, [(s, a) for s, a, *_ in mine])  # type: ignore[misc]
        assert b.status == expected.status, (b, mine)
        if expected.status == "open":
            assert b.payout_cents is None
        else:
            assert b.payout_cents == expected.payout_cents, (b, mine)
        for status, _, _, market_status, _ in mine:
            if market_status == "settled" and b.status != "lost":
                assert status in ("won", "lost", "push"), (b, mine)


def test_single_bets_still_resolve_exactly(season: SeasonRun) -> None:
    with season.engine.connect() as conn:
        rows = conn.execute(
            select(
                Bet.status,
                Bet.stake_cents,
                Bet.potential_payout_cents,
                Bet.payout_cents,
                BetLeg.status.label("leg"),
                Market.status.label("market"),
            )
            .join(BetLeg, BetLeg.bet_id == Bet.id)
            .join(Market, Market.id == BetLeg.market_id)
            .where(Bet.kind == "single")
        ).all()
    for r in rows:
        assert r.status == r.leg
        if r.market == "settled":
            expected = {"won": r.potential_payout_cents, "push": r.stake_cents, "lost": 0}
            assert r.payout_cents == expected[r.status], r
