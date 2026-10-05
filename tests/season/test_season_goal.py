"""STAGE14 measure: a simulated season on the `goal-in-30-days` preset ends, freezes, and
a new season starts cleanly. Time moves only through `sim.advance` (the real worker jobs:
sync -> goal watch -> drops -> lock -> settle -> pools), with scripted bettors, parlays
and two pools: one due before the goal, one after it."""

from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pytest
from sqlalchemy import Engine, func, select, update

from app.core.clock import SimClock, SystemClock
from app.core.config import Settings
from app.core.db import immediate, make_engine
from app.core.migrations import upgrade_to_head
from app.models import (
    Account,
    Bet,
    InstanceSettingsRow,
    LedgerEntry,
    Market,
    OddsVersion,
    OutboxMessage,
    Pool,
    Season,
    Selection,
)
from app.services import instance, ledger, pools, season, sim
from app.services.admin import Actor
from app.services.bets import BetRejected, place_bet, place_parlay
from app.services.observations import canonical_weigh_ins
from app.services.users import ensure_player
from tests.integration.world import create_admin

pytestmark = pytest.mark.season

NY = ZoneInfo("America/New_York")
START = date(2026, 10, 1)
GOAL_X10 = 1929  # the preset's 87.5 kg goal in lb
PLAYERS = 4


@dataclass
class GoalSeason:
    engine: Engine
    settings: Settings
    goal_day: date | None = None
    days: int = 0
    placed: Counter[str] = field(default_factory=Counter)
    ending: dict[int, int] = field(default_factory=dict)
    pools: tuple[int, int] = (0, 0)
    season2: int | None = None
    season2_markets: int = 0


def at(day: date, wall: time) -> datetime:
    return datetime.combine(day, wall, tzinfo=NY).astimezone(UTC)


def _open_legs(engine: Engine, now: datetime) -> list[tuple[int, int, int, frozenset[str]]]:
    with engine.connect() as conn:
        rows = conn.execute(
            select(
                Selection.id,
                OddsVersion.id,
                Market.id,
                Market.correlation_keys,
                Selection.side,
                OddsVersion.odds,
            )
            .join(Market, Market.id == Selection.market_id)
            .join(OddsVersion, (OddsVersion.market_id == Market.id) & OddsVersion.is_current)
            .where(Market.status == "open", Market.lock_at > now)
            .order_by(Selection.id)
        ).all()
    return [
        (s, v, m, frozenset(k)) for s, v, m, k, side, odds in rows if odds.get(side) is not None
    ]


@pytest.fixture(scope="module")
def run(tmp_path_factory: pytest.TempPathFactory) -> Iterator[GoalSeason]:
    tmp = tmp_path_factory.mktemp("goal-season")
    settings = Settings(
        app_env="dev",
        data_dir=tmp,
        data_provider="simulated",
        log_format="console",
        log_level="WARNING",
    )
    engine = make_engine(settings.db_url)
    upgrade_to_head(engine, Path(tmp) / ".migrate.lock")
    start = at(START, time(0, 30))
    clock = SimClock(start)
    with immediate(engine) as conn:
        sim.ensure_state(conn, clock, seed=7, tz_name="America/New_York")
    sim.reseed(engine, settings, preset="goal-in-30-days", seed=7)
    with immediate(engine) as conn:
        season_id = ledger.open_season(conn, clock)
        conn.execute(
            update(Season)
            .where(Season.id == season_id)
            .values(start_weight_x10=2028, goal_weight_x10=GOAL_X10, direction="down")
        )
        config = instance.ensure(conn, clock, settings)
        conn.execute(
            update(InstanceSettingsRow).values(
                setup_completed_at=start,
                flags=config.flags
                | {"props_futures": True, "parlays": True, "special_events": True},
            )
        )
        users = []
        for n in range(1, PLAYERS + 1):
            user = ensure_player(conn, clock, f"b{n}@example.invalid", f"B{n}")
            account = ledger.open_player_account(conn, clock, season_id, user)
            ledger.grant_starting(conn, clock, account, 100_000, idempotency_key=f"g:{user}")
            users.append(user)
    admin_id = create_admin(
        engine, SystemClock(), email="a@example.invalid", display_name="A", password="x" * 12
    )
    out = GoalSeason(engine, settings)
    rng = np.random.default_rng(5)
    now = start
    for k in range(70):
        day = START + timedelta(days=k)
        target = at(day, time(12, 0))
        sim.advance(engine, settings, target - now)
        now = target
        with engine.connect() as conn:
            frozen = instance.current_state(conn) == instance.FROZEN
        if frozen:
            break
        out.days = k + 1
        if k == 2:  # two pots: one due mid-season, one long after the goal
            with engine.connect() as conn:
                cfg = instance.read(conn)
            assert cfg is not None
            ids = []
            for title, days_out in (("Mid pot", 12), ("Late pot", 60)):
                raw = {
                    "title": title,
                    "question": f"Weight in {days_out} days?",
                    "target_date": (day + timedelta(days=days_out)).isoformat(),
                    "buy_in_cents": 2_000,
                }
                ids.append(
                    pools.create(
                        engine, SimClock(now), pools.validate(cfg, now, raw), actor_id=None
                    )
                )
            out.pools = (ids[0], ids[1])
            with engine.connect() as conn:
                latest = canonical_weigh_ins(conn, day - timedelta(days=5), day)[-1].value
            for i, user in enumerate(users):
                pools.enter(
                    engine,
                    SimClock(now),
                    user_id=user,
                    pool_id=ids[0],
                    guess_x10=latest - 20 - 15 * i,
                )
                pools.enter(
                    engine,
                    SimClock(now),
                    user_id=user,
                    pool_id=ids[1],
                    guess_x10=latest - 80 - 5 * i,
                )
        legs = _open_legs(engine, now)
        for user in users:
            if not legs or rng.random() > 0.7:
                continue
            sel, ver, _, _ = legs[int(rng.integers(len(legs)))]
            try:
                place_bet(
                    engine,
                    SimClock(now),
                    user_id=user,
                    selection_id=sel,
                    odds_version_id=ver,
                    stake_cents=int(rng.integers(100, 1_500)),
                    client_key=f"s{k}u{user}",
                )
                out.placed["single"] += 1
            except BetRejected as exc:
                out.placed[f"refused:{exc.reason}"] += 1
            if rng.random() < 0.3:
                chosen: list[tuple[int, int, int, frozenset[str]]] = []
                for i in rng.permutation(len(legs)):
                    leg = legs[int(i)]
                    if all(leg[2] != c[2] and not (leg[3] & c[3]) for c in chosen):
                        chosen.append(leg)
                    if len(chosen) == 2:
                        break
                if len(chosen) == 2:
                    try:
                        place_parlay(
                            engine,
                            SimClock(now),
                            user_id=user,
                            legs=[(c[0], c[1]) for c in chosen],
                            stake_cents=500,
                            client_key=f"p{k}u{user}",
                        )
                        out.placed["parlay"] += 1
                    except BetRejected as exc:
                        out.placed[f"refused:{exc.reason}"] += 1
    with engine.connect() as conn:
        s = conn.execute(select(Season.goal_reached_at, Season.goal_observation_id)).first()
        if s is not None and s.goal_reached_at is not None:
            out.goal_day = s.goal_reached_at.astimezone(NY).date()
        out.ending = {
            u: b
            for u, b in conn.execute(
                select(Account.user_id, Account.balance_cents).where(Account.kind == "player")
            )
            if u is not None
        }
    # The admin starts season 2 from the frozen state; the next day's drop posts markets.
    with engine.connect() as conn:
        last = canonical_weigh_ins(conn, START, START + timedelta(days=90))[-1].value
    out.season2 = season.new_season(
        engine, SimClock(now), Actor(admin_id), start_x10=last, goal_x10=last - 50
    )
    tomorrow = at(START + timedelta(days=out.days + 1), time(12, 0))
    sim.advance(engine, settings, tomorrow - now)
    with engine.connect() as conn:
        out.season2_markets = int(
            conn.execute(select(func.count()).where(Market.season_id == out.season2)).scalar_one()
        )
    yield out
    engine.dispose()


def test_goal_reached_freezes_and_settles_everything(run: GoalSeason) -> None:
    print(
        f"\ngoal season: goal reached {run.goal_day} after {run.days} days; bets {dict(run.placed)}"
    )
    assert run.goal_day is not None and 15 <= run.days <= 50
    with run.engine.connect() as conn:
        s1 = conn.execute(select(Season).where(Season.number == 1)).one()
        markets = Counter(
            conn.execute(select(Market.status).where(Market.season_id == s1.id)).scalars()
        )
        bets = Counter(conn.execute(select(Bet.status).where(Bet.season_id == s1.id)).scalars())
        mid, late = (
            conn.execute(select(Pool.status, Pool.outcome).where(Pool.id == p)).one()
            for p in run.pools
        )
        posts = (
            conn.execute(
                select(OutboxMessage.payload).where(OutboxMessage.category == "goal_reached")
            )
            .scalars()
            .all()
        )
        escrows = (
            conn.execute(select(Account.balance_cents).where(Account.kind == "pool"))
            .scalars()
            .all()
        )
        assert ledger.verify(conn).ok
    print(
        f"season 1 markets {dict(markets)}; bets {dict(bets)}; "
        f"pools mid={mid.status} late={late.status}"
    )
    assert s1.status == "ended" and s1.goal_observation_id is not None
    assert (
        set(markets) <= {"settled", "voided"} and markets["settled"] > 0 and markets["voided"] > 0
    )
    assert "open" not in bets and run.placed["single"] > 20
    assert mid.status == "settled" and late.status == "refunded"  # paid out; after the goal
    assert late.outcome["reason"] == "goal_reached"
    assert [p["kind"] for p in posts] == ["goal_reached", "season_start"]
    assert posts[0]["value_x10"] <= GOAL_X10
    assert set(escrows) == {0}


def test_new_season_carries_balances_and_resumes(run: GoalSeason) -> None:
    with run.engine.connect() as conn:
        s2 = conn.execute(select(Season).where(Season.id == run.season2)).one()
        accounts = {
            u: (b, p)
            for u, b, p in conn.execute(
                select(Account.user_id, Account.balance_cents, Account.pnl_cents).where(
                    Account.season_id == run.season2, Account.kind == "player"
                )
            )
            if u is not None
        }
        carried = conn.execute(
            select(func.sum(LedgerEntry.amount_cents))
            .join(Account, Account.id == LedgerEntry.account_id)
            .where(
                Account.season_id == run.season2,
                LedgerEntry.kind == "season_carry",
                Account.kind == "player",
            )
        ).scalar_one()
        state = instance.current_state(conn)
        assert ledger.verify(conn).ok
    print(
        f"season 2: {len(accounts)} accounts, carried {carried} cents, "
        f"{run.season2_markets} markets"
    )
    assert (s2.number, s2.status, state) == (2, "active", instance.ACTIVE)
    assert run.season2_markets > 0  # the next daily drop posted in season 2
    assert carried == sum(run.ending.values())
    for user, (balance, pnl) in accounts.items():
        assert pnl == 0 or balance != run.ending[user]  # P&L restarted (bets may move it)
