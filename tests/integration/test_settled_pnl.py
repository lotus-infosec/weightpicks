"""Issue #42: the public P&L counts only finished bets and events, and the leaderboard ranks
by it with Wins instead of Balance and Busts. The ledger itself is unchanged."""

from datetime import date

from sqlalchemy import select

from app.models import Account
from app.services import leaderboard, ledger, pools, settlement
from app.services.ledger import active_season_id
from tests.integration.test_bets_settlement import (
    bet,
    change,
    player,
    to_settle_time,
    weight_market,
)
from tests.integration.test_pools import TARGET, after_target, make
from tests.integration.world import World


def board(w: World) -> dict[int, leaderboard.Standing]:
    with w.engine.connect() as conn:
        return {s.user_id: s for s in leaderboard.standings(conn, active_season_id(conn))}


def ledger_pnl(w: World, user: int) -> int:
    with w.engine.connect() as conn:
        return int(conn.execute(select(Account.pnl_cents).where(Account.user_id == user)).one()[0])


def test_placing_a_bet_does_not_move_pnl_settling_does(world: World) -> None:
    a, b = player(world, 1), player(world, 2)
    actual = change(date(2026, 10, 5), date(2026, 10, 6))
    market = weight_market(world, actual - 5)  # the change ends above the line: over wins
    bet(world, a, market, "over", stake=2_000)
    bet(world, b, market, "under", stake=1_000)
    standings = board(world)
    assert standings[a].pnl_cents == standings[b].pnl_cents == 0  # nothing finished yet
    assert ledger_pnl(world, a) == -2_000  # the ledger still records the stake
    assert standings[a].open_bets == 1

    to_settle_time(world)
    assert settlement.settle_market(world.engine, world.clock, market).reason == "over"
    standings = board(world)
    assert standings[a].pnl_cents == ledger_pnl(world, a) > 0
    assert standings[b].pnl_cents == ledger_pnl(world, b) == -1_000
    assert (standings[a].wins, standings[b].wins) == (1, 0)
    assert [s.user_id for s in sorted(standings.values(), key=lambda s: s.rank)] == [a, b]
    with world.engine.connect() as conn:
        assert ledger.verify(conn).ok


def test_events_count_once_finished_and_a_win_counts(world: World) -> None:
    from tests.integration.test_bets_settlement import series

    actual = series()[TARGET]
    a, b = player(world, 1), player(world, 2)
    pool_id = make(world, buy_in_cents=1_000)
    pools.enter(world.engine, world.clock, user_id=a, pool_id=pool_id, guess_x10=actual - 1)
    pools.enter(world.engine, world.clock, user_id=b, pool_id=pool_id, guess_x10=actual + 5)
    assert board(world)[b].pnl_cents == 0  # the buy-in isn't P&L until the pot is decided
    after_target(world)
    assert pools.tick(world.engine, world.clock).finished == 1
    standings = board(world)
    assert (standings[a].pnl_cents, standings[a].wins) == (1_000, 1)
    assert (standings[b].pnl_cents, standings[b].wins) == (-1_000, 0)
    assert standings[a].pnl_cents == ledger_pnl(world, a)


def test_a_refunded_event_is_zero(world: World) -> None:
    from tests.integration.test_bets_settlement import series

    a = player(world, 1)
    pool_id = make(world, buy_in_cents=1_000)
    pools.enter(world.engine, world.clock, user_id=a, pool_id=pool_id, guess_x10=series()[TARGET])
    with world.engine.begin() as conn:
        pools.refund_pool(conn, world.clock, pool_id, "admin_void")
    assert board(world)[a].pnl_cents == 0 == ledger_pnl(world, a)
