"""Issue #44: players see why a bet was voided or pushed, in My Bets and in Discord."""

from datetime import date

from alembic import command
from sqlalchemy import select

from app.core.migrations import _config
from app.models import Market, OutboxMessage
from app.services import admin, board, settlement
from app.services.bets import place_parlay
from tests.integration.test_bets_settlement import (
    bet,
    change,
    player,
    selection,
    to_settle_time,
    weight_market,
)
from tests.integration.test_season import admin_actor, set_goal
from tests.integration.world import World


def mine(w: World, user: int) -> dict[int, board.BetRow]:
    with w.engine.connect() as conn:
        return {b.bet_id: b for b in board.my_bets(conn, user)}


def test_admin_void_reason_reaches_my_bets_and_discord(world: World) -> None:
    p = player(world, 1)
    market = weight_market(world, -5, day=date(2026, 10, 7))
    placed = bet(world, p, market, "over")
    admin.void_market(world.engine, world.clock, admin_actor(world), market, "  Scale broke  ")
    row = mine(world, p)[placed.bet_id]
    assert (row.status, row.reason) == ("void", "Voided by the admin: Scale broke")
    with world.engine.connect() as conn:
        stored = conn.execute(
            select(Market.void_reason, Market.void_note).where(Market.id == market)
        ).one()
        payloads = [
            p
            for (p,) in conn.execute(
                select(OutboxMessage.payload).where(
                    OutboxMessage.category.in_(("market_settlements", "bet_results"))
                )
            )
        ]
    assert tuple(stored) == ("admin", "Scale broke")
    assert {p["reason"] for p in payloads} == {"Voided by the admin: Scale broke"}


def test_push_shows_the_settlement_reason(world: World) -> None:
    p = player(world, 1)
    market = weight_market(world, change(date(2026, 10, 5), date(2026, 10, 6)))
    placed = bet(world, p, market, "over")
    to_settle_time(world)
    assert settlement.settle_market(world.engine, world.clock, market).reason == "tie"
    row = mine(world, p)[placed.bet_id]
    assert (row.status, row.reason) == ("push", "Push: an exact tie with the line")


def test_parlay_legs_carry_their_reason(world: World) -> None:
    set_goal(world)  # also turns parlays on
    p = player(world, 1)
    keep = weight_market(world, -5, day=date(2026, 10, 7))
    gone = weight_market(world, -5, day=date(2026, 10, 9))
    placed = place_parlay(
        world.engine,
        world.clock,
        user_id=p,
        legs=[selection(world, keep, "over"), selection(world, gone, "over")],
        stake_cents=1_000,
        client_key="two-legs",
    )
    admin.void_market(world.engine, world.clock, admin_actor(world), gone, "Duplicate market")
    row = mine(world, p)[placed.bet_id]
    reasons = {leg.market_id: leg.reason for leg in row.legs}
    assert reasons == {keep: None, gone: "Voided by the admin: Duplicate market"}
    assert row.status == "open" and row.reason is None


def test_migration_fills_reasons_for_older_voids(world: World) -> None:
    market = weight_market(world, -5, day=date(2026, 10, 7))
    admin.void_market(world.engine, world.clock, admin_actor(world), market, "Old reason")
    with world.engine.begin() as conn:
        command.downgrade(_config(conn), "0013")
    with world.engine.begin() as conn:
        command.upgrade(_config(conn), "head")
    with world.engine.connect() as conn:
        stored = conn.execute(
            select(Market.void_reason, Market.void_note).where(Market.id == market)
        ).one()
    assert tuple(stored) == ("admin", "Old reason")
