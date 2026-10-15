"""Parlays end to end: placement rules and settlement through real markets."""

from datetime import date

import pytest
from sqlalchemy import select, update

from app.core.db import immediate
from app.domain import parlay
from app.domain.money import payout_cents
from app.models import Bet, BetLeg, InstanceSettingsRow, OutboxMessage
from app.services import admin, audit, board, ledger, settlement
from app.services.bets import BetRejected, place_parlay
from tests.integration.test_bets_settlement import (
    account,
    change,
    player,
    selection,
    to_settle_time,
    weight_market,
)
from tests.integration.world import World

D5, D6, D7 = (date(2026, 10, d) for d in range(5, 8))
STAKE = 1_000


def flags(w: World, **values: bool) -> None:
    with immediate(w.engine) as conn:
        current = conn.execute(select(InstanceSettingsRow.flags)).scalar_one() or {}
        conn.execute(update(InstanceSettingsRow).values(flags=dict(current) | values))


def market(w: World, d0: date, outcome: str, odds: tuple[int, int] = (-110, -110)) -> int:
    """A daily weight market on d0 -> d0+1 whose Over wins, loses or ties (push)."""
    actual = change(d0, date.fromordinal(d0.toordinal() + 1))
    line = {"over": actual - 5, "under": actual + 5, "push": actual}[outcome]
    return weight_market(w, line, odds, day=d0)


def legs(w: World, *markets: int, side: str = "over") -> list[tuple[int, int]]:
    return [selection(w, m, side) for m in markets]


def bet(w: World, user: int, picks: list[tuple[int, int]], key: str, stake: int = STAKE):  # type: ignore[no-untyped-def]
    return place_parlay(
        w.engine, w.clock, user_id=user, legs=picks, stake_cents=stake, client_key=key
    )


def rejected(fn) -> str:  # type: ignore[no-untyped-def]
    with pytest.raises(BetRejected) as info:
        fn()
    return info.value.reason


def test_placement_rules(world: World) -> None:
    w = world
    user = player(w, 1)
    a, b, overlap = market(w, D5, "over"), market(w, D7, "over"), market(w, D6, "over")
    assert rejected(lambda: bet(w, user, legs(w, a, b), "k0")) == "parlays_off"
    flags(w, parlays=True)
    assert rejected(lambda: bet(w, user, legs(w, a), "k1")) == "leg_count"
    both_sides = [selection(w, a, "over"), selection(w, a, "under")]
    assert rejected(lambda: bet(w, user, both_sides, "k2")) == "same_market"
    assert rejected(lambda: bet(w, user, legs(w, a, overlap), "k3")) == "correlated"  # share Oct 6
    longshots = (
        weight_market(w, 0, (2000, -5000), day=date(2026, 10, 11)),
        weight_market(w, 0, (2000, -5000), day=date(2026, 10, 13)),
    )
    assert rejected(lambda: bet(w, user, legs(w, *longshots), "k4")) == "over_cap"  # 21 x 21 = 441x
    assert (
        rejected(lambda: bet(w, user, legs(w, a, b), "k5", stake=10_000_000))
        == "insufficient_funds"
    )
    placed = bet(w, user, legs(w, a, b), "k6")
    assert (
        placed.potential_payout_cents == parlay.potential_payout_cents(STAKE, [-110, -110]) == 3644
    )
    assert placed.combined_american == 264
    assert bet(w, user, legs(w, a, b), "k6").replayed  # same client key, same legs
    assert rejected(lambda: bet(w, user, legs(w, b, a), "k6")) == "client_key_conflict"
    assert account(w, user) == (100_000 - STAKE, -STAKE)


def status(w: World, bet_id: int) -> tuple[str, int | None, list[str]]:
    with w.engine.connect() as conn:
        b = conn.execute(select(Bet.status, Bet.payout_cents).where(Bet.id == bet_id)).one()
        leg_status = (
            conn.execute(select(BetLeg.status).where(BetLeg.bet_id == bet_id).order_by(BetLeg.id))
            .scalars()
            .all()
        )
    return b.status, b.payout_cents, list(leg_status)


def settle_day(w: World, d1: date) -> None:
    to_settle_time(w, d1)
    settlement.settle_due(w.engine, w.clock)


def test_settlement_truths_through_real_markets(world: World) -> None:
    w = world
    flags(w, parlays=True)
    user = player(w, 1)
    day = {d: date(2026, 10, d) for d in (5, 7, 11, 13, 15, 17, 19, 21)}
    both_win = bet(w, user, legs(w, market(w, day[5], "over"), market(w, day[7], "over")), "a")
    early_loss = bet(w, user, legs(w, market(w, day[11], "under"), market(w, day[13], "over")), "b")
    one_push = bet(w, user, legs(w, market(w, day[15], "push"), market(w, day[17], "over")), "c")
    all_push = bet(w, user, legs(w, market(w, day[19], "push"), market(w, day[21], "push")), "d")

    settle_day(w, date(2026, 10, 12))  # everything up to the Oct 11 -> 12 market
    assert status(w, both_win.bet_id) == (
        "won",
        int(STAKE * parlay.combined([-110, -110])),
        ["won", "won"],
    )
    # Loses at once; the other leg stays open and settles with its own result later.
    assert status(w, early_loss.bet_id) == ("lost", 0, ["lost", "open"])
    settle_day(w, date(2026, 10, 14))
    assert status(w, early_loss.bet_id) == ("lost", 0, ["lost", "won"])
    settle_day(w, date(2026, 10, 16))
    assert status(w, one_push.bet_id)[2] == ["push", "open"]
    settle_day(w, date(2026, 10, 22))
    assert status(w, one_push.bet_id) == ("won", payout_cents(STAKE, -110), ["push", "won"])
    assert status(w, all_push.bet_id) == ("push", STAKE, ["push", "push"])  # refunded
    expected = (
        100_000
        - 4 * STAKE
        + int(STAKE * parlay.combined([-110, -110]))
        + payout_cents(STAKE, -110)
        + STAKE
    )
    assert account(w, user)[0] == expected
    with w.engine.connect() as conn:
        posts = conn.execute(
            select(OutboxMessage.category, OutboxMessage.payload).where(
                OutboxMessage.dedupe_key.like("bet_result:%")
            )
        ).all()
    parlay_posts = {p.payload["bet_id"]: p.category for p in posts if p.payload.get("kind")}
    # Hits and busted parlays go to their own Discord category.
    assert set(parlay_posts.values()) == {"parlay_results"}
    assert set(parlay_posts) == {b.bet_id for b in (both_win, early_loss, one_push, all_push)}


def test_a_voided_leg_voids_the_whole_parlay(world: World) -> None:
    """Issue #69: one voided leg refunds the whole open parlay (a pushed leg still just
    drops out); a parlay that is already decided doesn't change."""
    w = world
    flags(w, parlays=True)
    user = player(w, 1)
    keep, gone = market(w, D5, "over"), market(w, D7, "over")
    bet_id = bet(w, user, legs(w, keep, gone), "v").bet_id
    balance = account(w, user)[0]
    admin.void_market(w.engine, w.clock, audit.Actor(user_id=user), gone, "bad line")
    assert status(w, bet_id) == ("void", STAKE, ["void", "void"])
    assert account(w, user)[0] == balance + STAKE
    with w.engine.connect() as conn:
        post = conn.execute(
            select(OutboxMessage.category, OutboxMessage.payload).where(
                OutboxMessage.dedupe_key == f"bet_result:{bet_id}"
            )
        ).one()
        assert ledger.verify(conn).ok
    assert post.category == "parlay_results"
    assert (post.payload["result"], post.payload["legs"]) == ("void", 2)
    settle_day(w, D6)  # the other leg's market still settles; the parlay stays refunded
    assert status(w, bet_id)[:2] == ("void", STAKE)


def test_voiding_a_leg_of_a_lost_parlay_changes_nothing(world: World) -> None:
    w = world
    flags(w, parlays=True)
    user = player(w, 1)
    loser, later = market(w, D5, "under"), market(w, D7, "over")
    bet_id = bet(w, user, legs(w, loser, later), "l").bet_id
    settle_day(w, D6)
    assert status(w, bet_id)[0] == "lost"
    balance = account(w, user)[0]
    admin.void_market(w.engine, w.clock, audit.Actor(user_id=user), later, "bad line")
    assert status(w, bet_id)[0] == "lost" and account(w, user)[0] == balance
    # The open leg on the voided market is closed as void (no refund: it was decided).
    assert status(w, bet_id)[2] == ["lost", "void"]


def test_legs_stored_void_by_an_earlier_loss_show_their_market_result(world: World) -> None:
    """Before the fix, a lost parlay's other legs were stored as "void"; My Bets and the
    admin page show them as their market did instead (open, then its result)."""
    w = world
    flags(w, parlays=True)
    user = player(w, 1)
    first, second = market(w, D5, "under"), market(w, D7, "over")
    bet_id = bet(w, user, legs(w, first, second), "old").bet_id
    with immediate(w.engine) as conn:  # what the old code stored once the parlay lost
        conn.execute(
            update(BetLeg)
            .where(BetLeg.bet_id == bet_id, BetLeg.market_id == second)
            .values(status="void")
        )

    def shown() -> list[str]:
        with w.engine.connect() as conn:
            row = {b.bet_id: b for b in board.my_bets(conn, user)}[bet_id]
        return [leg.status for leg in row.legs] + [row.status]

    assert shown()[1] == "open"
    settle_day(w, date(2026, 10, 8))
    assert shown() == ["lost", "won", "lost"]
