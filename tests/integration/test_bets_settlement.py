from datetime import date, timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, func, insert, select, update

from app.calibration import canonical_tenths
from app.core.clock import SimClock
from app.core.db import immediate
from app.domain.lines import Pricing
from app.domain.markets import Timeframe, drop_specs
from app.models import (
    Account,
    Bet,
    BetLeg,
    InstanceSettingsRow,
    LedgerTxn,
    Market,
    OddsVersion,
    OutboxMessage,
    Selection,
    Settlement,
    User,
)
from app.providers.simulated import SimulatedProvider
from app.services import ledger, markets, settlement
from app.services.bets import BetRejected, place_bet
from app.services.users import ensure_player
from tests.integration.world import NY, World, local, sync_sim

STAKE = 10_000  # $100


def series() -> dict[date, int]:
    """The simulator's canonical weigh-ins (same provider as `sync_sim`)."""
    provider = SimulatedProvider(preset="steady-loser", seed=3, anchor_date=date(2026, 9, 1), tz=NY)
    return canonical_tenths(provider, 60, NY, "lb")


def change(d0: date, d1: date) -> int:
    s = series()
    return s[d1] - s[d0]


def player(w: World, n: int, grant: int = 100_000) -> int:
    with immediate(w.engine) as conn:
        season = ledger.active_season_id(conn)
        assert season is not None
        user = ensure_player(conn, w.clock, f"p{n}@example.invalid", f"P{n}")
        account = ledger.open_player_account(conn, w.clock, season, user)
        ledger.grant_starting(conn, w.clock, account, grant, idempotency_key=f"grant:{user}")
    return user


def weight_market(
    w: World,
    line_x10: int,
    odds: tuple[int | None, int | None] = (-110, -110),
    day: date = date(2026, 10, 5),
) -> int:
    """A daily weight market d0=day, d1=day+1 at an exact line and odds."""
    spec = drop_specs(
        Timeframe.DAILY, day, schedule=w.config.schedule, tz=NY, unit="lb", enabled_metrics=()
    )[0]
    pricing = Pricing(line_x10, 0.5, 0.5, 0.5, odds[0], odds[1], {"model": "test"})
    with immediate(w.engine) as conn:
        season = ledger.active_season_id(conn)
        assert season is not None
        return markets.insert_market(
            conn, season_id=season, spec=spec, pricing=pricing, now=w.clock.now()
        )


def selection(w: World, market_id: int, side: str) -> tuple[int, int]:
    with w.engine.connect() as conn:
        sel = conn.execute(
            select(Selection.id).where(Selection.market_id == market_id, Selection.side == side)
        ).scalar_one()
        ver = conn.execute(
            select(OddsVersion.id).where(OddsVersion.market_id == market_id, OddsVersion.is_current)
        ).scalar_one()
    return sel, ver


def bet(
    w: World, user: int, market_id: int, side: str, stake: int = STAKE, key: str | None = None
) -> Any:
    sel, ver = selection(w, market_id, side)
    return place_bet(
        w.engine,
        w.clock,
        user_id=user,
        selection_id=sel,
        odds_version_id=ver,
        stake_cents=stake,
        client_key=key or f"{user}-{market_id}-{side}-{stake}",
    )


def account(w: World, user: int) -> tuple[int, int]:
    with w.engine.connect() as conn:
        row = conn.execute(
            select(Account.balance_cents, Account.pnl_cents).where(Account.user_id == user)
        ).one()
    return row.balance_cents, row.pnl_cents


def outbox(w: World) -> list[tuple[str, str]]:
    with w.engine.connect() as conn:
        return [
            (c, k)
            for c, k in conn.execute(select(OutboxMessage.category, OutboxMessage.dedupe_key))
        ]


def count(engine: Engine, model: Any) -> int:
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(model)).scalar_one()


def to_settle_time(w: World, day: date = date(2026, 10, 6)) -> None:
    """After `day`'s weigh-in window: sync, then run the lock pass."""
    w.clock.set(local(day.year, day.month, day.day, 13, 30))
    sync_sim(w.engine, w.clock)
    markets.lock_due(w.engine, w.clock.now())


# ---- place_bet ---------------------------------------------------------------------------


def test_place_bet_stakes_and_queues_notifications(world: World) -> None:
    user = player(world, 1)
    market = weight_market(world, -5)
    placed = bet(world, user, market, "over")
    assert (placed.american, placed.line_x10, placed.stake_cents) == (-110, -5, STAKE)
    assert placed.potential_payout_cents == 19_090 and not placed.replayed
    assert account(world, user) == (90_000, -10_000)
    assert outbox(world) == [("bets_placed", f"bet_placed:{placed.bet_id}")]
    with world.engine.connect() as conn:
        leg = conn.execute(select(BetLeg)).one()
        b = conn.execute(select(Bet)).one()
    assert (leg.american, leg.line_x10, leg.status, b.status, b.kind) == (
        -110,
        -5,
        "open",
        "open",
        "single",
    )


def test_high_roller_bet_is_announced(world: World) -> None:
    user = player(world, 1)
    placed = bet(world, user, weight_market(world, -5), "under", stake=50_000)
    assert {c for c, _ in outbox(world)} == {"bets_placed", "high_roller"}
    assert placed.potential_payout_cents == 95_454  # 50,000 + floor(50,000 * 100 / 110)


def test_client_key_replay_and_conflict(world: World) -> None:
    user = player(world, 1)
    market = weight_market(world, -5)
    first = bet(world, user, market, "over", key="slip-1")
    again = bet(world, user, market, "over", key="slip-1")
    assert again.replayed and again.bet_id == first.bet_id
    assert count(world.engine, Bet) == 1 and account(world, user)[0] == 90_000
    with pytest.raises(BetRejected) as exc:
        bet(world, user, market, "under", key="slip-1")
    assert exc.value.reason == "client_key_conflict"


def rejected(fn: Any) -> str:
    with pytest.raises(BetRejected) as exc:
        fn()
    return exc.value.reason


def test_bet_rejections(world: World) -> None:
    user = player(world, 1, grant=20_000)
    market = weight_market(world, -5)

    def over(stake: Any) -> Any:
        return bet(world, user, market, "over", stake=stake)

    assert rejected(lambda: over(stake=99)) == "below_minimum"
    assert rejected(lambda: over(stake=1.5)) == "invalid_stake"
    assert rejected(lambda: over(stake=20_001)) == "insufficient_funds"
    assert count(world.engine, Bet) == 0 and account(world, user) == (20_000, 0)  # rolled back

    # Side not offered.
    one_sided = weight_market(world, -15, odds=(-2000, None), day=date(2026, 10, 6))
    assert rejected(lambda: bet(world, user, one_sided, "under")) == "side_not_offered"

    # A newer odds version makes the pinned one stale.
    sel, old = selection(world, market, "over")
    with immediate(world.engine) as conn:
        conn.execute(update(OddsVersion).where(OddsVersion.id == old).values(is_current=False))
        conn.execute(
            insert(OddsVersion).values(
                market_id=market,
                version=2,
                line_x10=-5,
                odds={"over": -115, "under": -105},
                model_inputs={},
                is_current=True,
                created_at=world.clock.now(),
            )
        )
    stale = lambda: place_bet(  # noqa: E731
        world.engine,
        world.clock,
        user_id=user,
        selection_id=sel,
        odds_version_id=old,
        stake_cents=STAKE,
        client_key="stale",
    )
    assert rejected(stale) == "stale_odds"

    # Before the lock pass runs, lock_at itself refuses the bet.
    world.clock.set(local(2026, 10, 5, 22, 0))
    assert rejected(lambda: over(stake=500)) == "locked"
    markets.lock_due(world.engine, world.clock.now())
    world.clock.set(local(2026, 10, 5, 21, 0))  # even if the clock read earlier
    assert rejected(lambda: over(stake=500)) == "locked"


@pytest.mark.parametrize(
    ("role", "status", "reason"),
    [
        ("player", "frozen", "user_inactive"),
        ("player", "banned", "user_inactive"),
        ("admin", "active", "user_inactive"),
    ],
)
def test_inactive_users_and_the_admin_cannot_bet(
    world: World, role: str, status: str, reason: str
) -> None:
    user = player(world, 1)
    market = weight_market(world, -5)
    with immediate(world.engine) as conn:
        conn.execute(update(User).where(User.id == user).values(role=role, status=status))
    assert rejected(lambda: bet(world, user, market, "over")) == reason


def test_no_bets_while_frozen_or_without_an_account(world: World) -> None:
    market = weight_market(world, -5)
    with immediate(world.engine) as conn:
        stranger = ensure_player(conn, world.clock, "nobody@example.invalid", "Nobody")
    assert rejected(lambda: bet(world, stranger, market, "over")) == "no_account"
    user = player(world, 1)
    with immediate(world.engine) as conn:
        conn.execute(update(InstanceSettingsRow).values(instance_state="frozen"))
    assert rejected(lambda: bet(world, user, market, "over")) == "instance_frozen"


# ---- settlement --------------------------------------------------------------------------


def test_winner_is_paid_loser_is_not(world: World) -> None:
    d0, d1 = date(2026, 10, 5), date(2026, 10, 6)
    market = weight_market(world, change(d0, d1) - 5)  # Over wins by 0.5
    winner, loser = player(world, 1), player(world, 2)
    won = bet(world, winner, market, "over")
    lost = bet(world, loser, market, "under")
    to_settle_time(world)
    result = settlement.settle_market(world.engine, world.clock, market)
    assert result.settled and result.reason == "over" and result.bets == 2
    assert account(world, winner) == (100_000 + 9_090, 9_090)  # $100 at -110 -> +$90.90
    assert account(world, loser) == (90_000, -10_000)
    with world.engine.connect() as conn:
        statuses = dict(conn.execute(select(Bet.id, Bet.status)).all())
        payouts = dict(conn.execute(select(Bet.id, Bet.payout_cents)).all())
        row = conn.execute(select(Settlement)).one()
        m = conn.execute(select(Market.status)).scalar_one()
    assert statuses == {won.bet_id: "won", lost.bet_id: "lost"}
    assert payouts == {won.bet_id: 19_090, lost.bet_id: 0}
    assert m == "settled" and row.engine_version == "settle-1"
    assert row.outcome["winner"] == "over" and row.outcome["value_x10"] == change(d0, d1)
    assert [w["local_date"] for w in row.inputs["weigh_ins"]] == ["2026-10-05", "2026-10-06"]
    cats = sorted(c for c, _ in outbox(world))
    assert cats == [
        "bet_results",
        "bet_results",
        "bets_placed",
        "bets_placed",
        "market_settlements",
    ]
    with world.engine.connect() as conn:
        assert ledger.verify(conn).ok


def test_exact_tie_pushes_and_refunds(world: World) -> None:
    market = weight_market(world, change(date(2026, 10, 5), date(2026, 10, 6)))
    a, b = player(world, 1), player(world, 2)
    bet(world, a, market, "over")
    bet(world, b, market, "under")
    to_settle_time(world)
    assert settlement.settle_market(world.engine, world.clock, market).reason == "tie"
    assert account(world, a) == account(world, b) == (100_000, 0)
    with world.engine.connect() as conn:
        assert set(conn.execute(select(Bet.status)).scalars()) == {"push"}


def test_missing_weigh_in_pushes(world: World) -> None:
    s = series()
    day = next(d for d in sorted(s) if d >= date(2026, 9, 20) and d + timedelta(days=1) not in s)
    world.clock.set(local(day.year, day.month, day.day, 12))
    market = weight_market(world, -5, day=day)
    user = player(world, 1)
    bet(world, user, market, "over")
    to_settle_time(world, day + timedelta(days=1))
    assert settlement.settle_market(world.engine, world.clock, market).reason == "missing_weigh_in"
    assert account(world, user) == (100_000, 0)


def test_settlement_is_idempotent(world: World) -> None:
    market = weight_market(world, change(date(2026, 10, 5), date(2026, 10, 6)) - 5)
    user = player(world, 1)
    bet(world, user, market, "over")
    to_settle_time(world)
    assert settlement.settle_market(world.engine, world.clock, market).settled
    txns = count(world.engine, LedgerTxn)
    again = settlement.settle_market(world.engine, world.clock, market)
    assert (again.settled, again.reason) == (False, "already_settled")
    assert settlement.settle_due(world.engine, world.clock).settled == 0
    assert count(world.engine, LedgerTxn) == txns and count(world.engine, Settlement) == 1
    assert account(world, user)[1] == 9_090


def test_crash_mid_settle_rolls_everything_back(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    market = weight_market(world, change(date(2026, 10, 5), date(2026, 10, 6)) - 5)
    user = player(world, 1)
    bet(world, user, market, "over")
    to_settle_time(world)

    def boom(*args: Any, **kwargs: Any) -> int:
        raise RuntimeError("worker killed after the payout, before the status change")

    monkeypatch.setattr(settlement, "set_status", boom)
    with pytest.raises(RuntimeError):
        settlement.settle_market(world.engine, world.clock, market)
    assert account(world, user) == (90_000, -10_000)  # payout rolled back
    assert count(world.engine, Settlement) == 0
    with world.engine.connect() as conn:
        assert conn.execute(select(Bet.status)).scalar_one() == "open"
    monkeypatch.undo()
    assert settlement.settle_market(world.engine, world.clock, market).settled
    assert account(world, user) == (109_090, 9_090)


def test_stale_data_waits_and_alerts_once_at_the_deadline(world: World) -> None:
    market = weight_market(world, -5)
    user = player(world, 1)
    bet(world, user, market, "over")
    # Time passes but no sync runs after Oct 6's weigh-in window closes.
    world.clock.set(local(2026, 10, 6, 13, 30))
    markets.lock_due(world.engine, world.clock.now())
    first = settlement.settle_due(world.engine, world.clock)
    assert (first.settled, first.alerts) == (0, 0)
    assert (
        settlement.settle_market(world.engine, world.clock, market).reason == "no_sync_since_window"
    )
    world.clock.set(local(2026, 10, 7, 11, 0))  # settle_after + 24 h
    assert settlement.settle_due(world.engine, world.clock).alerts == 1
    assert settlement.settle_due(world.engine, world.clock).alerts == 0
    assert ("admin_alerts", f"stale_market:{market}") in outbox(world)
    with world.engine.connect() as conn:
        assert conn.execute(select(Market.status)).scalar_one() == "locked"  # never auto-pushed
    sync_sim(world.engine, world.clock)  # data finally arrives
    assert settlement.settle_due(world.engine, world.clock).settled == 1


def test_market_without_bets_settles(world: World) -> None:
    market = weight_market(world, -5)
    to_settle_time(world)
    assert settlement.settle_market(world.engine, world.clock, market).settled


def test_open_market_does_not_settle(world: World) -> None:
    market = weight_market(world, -5)
    world.clock.set(local(2026, 10, 5, 13))
    assert settlement.settle_market(world.engine, world.clock, market).reason == "status_open"


def test_count_markets_settle_from_daily_totals(world: World) -> None:
    world.clock.set(local(2026, 10, 4, 18))
    markets.drop(world.engine, world.clock, world.config, Timeframe.WEEKLY, date(2026, 10, 4))
    world.clock.set(local(2026, 10, 5, 12))
    markets.drop(world.engine, world.clock, world.config, Timeframe.DAILY, date(2026, 10, 5))
    world.clock.set(local(2026, 10, 12, 3))
    sync_sim(world.engine, world.clock)
    markets.lock_due(world.engine, world.clock.now())
    settled = settlement.settle_due(world.engine, world.clock)
    with world.engine.connect() as conn:
        rows = conn.execute(
            select(
                Market.timeframe,
                Market.metric,
                Market.status,
                Settlement.outcome,
                Settlement.inputs,
            ).join(Settlement, Settlement.market_id == Market.id)
        ).all()
    by = {(r.timeframe, r.metric): r for r in rows}
    assert settled.settled == len(rows) == 11  # 6 weekly + 5 daily
    workouts = by[("weekly", "workouts")]
    assert len(workouts.inputs["workouts"]) == 7
    assert workouts.outcome["value_x10"] == 10 * sum(workouts.inputs["workouts"].values())
    steps = by[("daily", "steps")]
    assert steps.outcome["reason"] in {"over", "under", "tie"}
    assert steps.outcome["value_x10"] == 10 * steps.inputs["totals"][0]["value"]
    with world.engine.connect() as conn:
        assert ledger.verify(conn).ok


def test_sim_clock_drives_lock_and_settle_jobs(world: World) -> None:
    """End to end through the worker jobs: drop, bet, lock, settle."""
    from app.worker.jobs import domain_jobs
    from app.worker.registry import run_due

    jobs = [j for j in domain_jobs(world.settings, world.config) if j.name != "garmin_sync"]
    user = player(world, 1)
    world.clock.set(local(2026, 10, 5, 11))
    run_due(jobs, world.engine, world.clock, seen={})
    with world.engine.connect() as conn:
        weight = conn.execute(
            select(Market.id).where(Market.metric == "weight", Market.timeframe == "daily")
        ).scalar_one()
    bet(world, user, weight, "over")
    clock: SimClock = world.clock
    for t in (local(2026, 10, 5, 22), local(2026, 10, 6, 13, 30)):
        clock.set(t)
        sync_sim(world.engine, clock)
        run_due(jobs, world.engine, clock, seen={})
    with world.engine.connect() as conn:
        assert (
            conn.execute(select(Market.status).where(Market.id == weight)).scalar_one() == "settled"
        )
        assert conn.execute(select(Bet.status)).scalar_one() in {"won", "lost", "push"}
