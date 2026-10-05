"""Goal Reached (D-011), freeze, and new seasons with carried balances (D-043)."""

from datetime import date
from typing import Any

import pytest
from sqlalchemy import func, select, update

from app.core.clock import SystemClock
from app.core.db import immediate
from app.domain.lines import Pricing
from app.domain.markets import TEMPLATES, Timeframe
from app.domain.props import FutureChangeParams, MilestoneParams
from app.models import (
    Account,
    AuditEntry,
    Bet,
    InstanceSettingsRow,
    LedgerEntry,
    Market,
    OutboxMessage,
    Pool,
    Season,
)
from app.services import auth, instance, ledger, markets, pools, season
from app.services.admin import Actor
from app.services.bets import BetRejected, place_parlay
from app.web.main import create_app
from app.worker.jobs.season import GoalWatchJob
from app.worker.registry import JobContext
from tests.integration import web
from tests.integration.test_bets_settlement import account, bet, player, selection, weight_market
from tests.integration.world import NY, World, create_admin, local, sync_sim

GOAL_DAY = date(2026, 10, 11)  # first canonical weigh-in at or under 220.0 lb (219.4)
GOAL_X10 = 2200


def set_goal(w: World, goal_x10: int = GOAL_X10, start_x10: int = 2216) -> None:
    with immediate(w.engine) as conn:
        conn.execute(
            update(Season).values(
                start_weight_x10=start_x10, goal_weight_x10=goal_x10, direction="down"
            )
        )
        flags = conn.execute(select(InstanceSettingsRow.flags)).scalar_one() or {}
        conn.execute(
            update(InstanceSettingsRow).values(
                flags=dict(flags) | {"parlays": True, "props_futures": True}
            )
        )


def yes_no_market(w: World, template: str, params: Any) -> int:
    t = TEMPLATES[template]
    spec = t.spec(params, Timeframe.PROP, w.config.schedule, NY, "lb")
    pricing = Pricing(None, 0.5, 0.5, 0.5, -110, -110, {"model": "test"})
    with immediate(w.engine) as conn:
        season_id = ledger.active_season_id(conn)
        assert season_id is not None
        return markets.insert_market(
            conn, season_id=season_id, spec=spec, pricing=pricing, now=w.clock.now(), origin="admin"
        )


def admin_actor(w: World) -> Actor:
    admin_id = create_admin(
        w.engine, SystemClock(), email="a@example.invalid", display_name="A", password=web.PASSWORD
    )
    return Actor(user_id=admin_id)


def statuses(w: World, *ids: int) -> list[str]:
    with w.engine.connect() as conn:
        rows = dict(conn.execute(select(Market.id, Market.status).where(Market.id.in_(ids))).all())
    return [rows[i] for i in ids]


@pytest.fixture
def mid_season(world: World) -> dict[str, Any]:
    """Mon Oct 5: bets on markets before, at and after the goal day; two pools."""
    w = world
    set_goal(w)
    p1, p2 = player(w, 1), player(w, 2)
    m = {
        "before": weight_market(w, -5, day=date(2026, 10, 8)),  # Oct 8 -> 9: settles
        "after": weight_market(w, -5, day=date(2026, 10, 12)),  # Oct 12 -> 13: voided
        "milestone": yes_no_market(
            w,
            "milestone_by",
            MilestoneParams(
                start=date(2026, 10, 6), deadline=date(2026, 10, 15), threshold_x10=2205
            ),
        ),
        "future": yes_no_market(
            w,
            "future_total_change",
            FutureChangeParams(
                created=date(2026, 10, 5),
                day=date(2026, 11, 5),
                season_start=date(2026, 10, 5),
                start_weight_x10=2216,
            ),
        ),
    }
    bet(w, p1, m["before"], "over")
    bet(w, p1, m["after"], "under")
    bet(w, p1, m["milestone"], "yes")
    bet(w, p1, m["future"], "over")
    parlay = place_parlay(
        w.engine,
        w.clock,
        user_id=p2,
        legs=[selection(w, m["before"], "over"), selection(w, m["after"], "over")],
        stake_cents=1_000,
        client_key="spanning",
    )
    raw = {"title": "Goal day pot", "question": "Weight on Oct 11?", "buy_in_cents": 2_000}
    with w.engine.connect() as conn:
        config = instance.read(conn)
    assert config is not None
    early = pools.create(
        w.engine,
        w.clock,
        pools.validate(config, w.clock.now(), raw | {"target_date": GOAL_DAY.isoformat()}),
        actor_id=None,
    )
    late = pools.create(
        w.engine,
        w.clock,
        pools.validate(config, w.clock.now(), raw | {"target_date": "2026-10-20", "title": "Late"}),
        actor_id=None,
    )
    for user, guess in ((p1, 2190), (p2, 2199)):
        pools.enter(w.engine, w.clock, user_id=user, pool_id=early, guess_x10=guess)
        pools.enter(w.engine, w.clock, user_id=user, pool_id=late, guess_x10=guess)
    return {"world": w, "p1": p1, "p2": p2, "m": m, "parlay": parlay.bet_id, "pools": (early, late)}


def to_goal_day(w: World) -> None:
    w.clock.set(local(2026, 10, 11, 13, 30))
    sync_sim(w.engine, w.clock)
    markets.lock_due(w.engine, w.clock.now())


def test_goal_reached_settles_voids_refunds_and_freezes(mid_season: dict[str, Any]) -> None:
    w, m = mid_season["world"], mid_season["m"]
    with w.engine.connect() as conn:
        assert season.detect(conn) is None  # not reached yet
    to_goal_day(w)
    job = GoalWatchJob()
    ctx = JobContext(w.engine, w.clock, w.clock.now(), "tick")
    assert job.run(ctx) == 1
    assert job.run(ctx) == 0  # same sync: not even re-checked
    with w.engine.connect() as conn:
        s = conn.execute(select(Season)).one()
        assert (s.status, s.goal_reached_at is not None) == ("frozen", True)
        assert s.goal_observation_id is not None
        assert instance.current_state(conn) == instance.FROZEN
        assert ledger.verify(conn).ok
        parlay = conn.execute(select(Bet.status).where(Bet.id == mid_season["parlay"])).scalar_one()
        pool_status = [
            conn.execute(select(Pool.status).where(Pool.id == p)).scalar_one()
            for p in mid_season["pools"]
        ]
        post = conn.execute(
            select(OutboxMessage.payload).where(OutboxMessage.category == "goal_reached")
        ).scalar_one()
        actions = conn.execute(select(AuditEntry.action)).scalars().all()
        left = conn.execute(
            select(func.count()).where(Market.status.in_(("open", "locked")))
        ).scalar_one()
    assert statuses(w, m["before"], m["milestone"], m["after"], m["future"]) == [
        "settled",
        "settled",  # the goal weigh-in crossed the milestone
        "voided",
        "voided",
    ]
    assert left == 0
    assert parlay in ("won", "lost")  # the voided leg dropped out; resolved on the other
    assert pool_status == ["settled", "refunded"]
    assert (post["value_x10"], post["day"], post["goal_x10"]) == (2194, "2026-10-11", GOAL_X10)
    assert "season.goal_reached" in actions
    # Reruns and the manual trigger are no-ops after the automatic run.
    assert season.goal_reached(w.engine, w.clock) is None
    assert season.goal_reached(w.engine, w.clock, actor=admin_actor(w)) is None
    with pytest.raises(BetRejected, match="instance_frozen"):
        bet(w, mid_season["p1"], m["after"], "over", key="late")


def test_manual_goal_reached_without_a_qualifying_weigh_in(world: World) -> None:
    w = world
    set_goal(w, goal_x10=1500)  # far away: detect never fires
    actor = admin_actor(w)
    result = season.goal_reached(w.engine, w.clock, actor=actor)
    assert result is not None
    with w.engine.connect() as conn:
        s = conn.execute(select(Season)).one()
        post = conn.execute(
            select(OutboxMessage.payload).where(OutboxMessage.category == "goal_reached")
        ).scalar_one()
        after = conn.execute(
            select(AuditEntry.after).where(AuditEntry.action == "season.goal_reached")
        ).scalar_one()
    assert (s.status, s.goal_observation_id) == ("frozen", None)
    assert post["manual"] is True and post["value_x10"] is None
    assert after["trigger"] == "manual"


def test_freeze_unfreeze_and_unfreeze_refused_after_goal(world: World) -> None:
    w = world
    actor = admin_actor(w)
    market = weight_market(w, -5, day=date(2026, 10, 8))
    season.set_frozen(w.engine, w.clock, actor, True)
    assert statuses(w, market) == ["locked"]  # freezing locks every open market
    season.set_frozen(w.engine, w.clock, actor, True)  # no-op
    season.set_frozen(w.engine, w.clock, actor, False)
    with w.engine.connect() as conn:
        assert instance.current_state(conn) == instance.ACTIVE
        actions = conn.execute(select(AuditEntry.action)).scalars().all()
    assert actions.count("instance.freeze") == 1 and "instance.unfreeze" in actions
    season.goal_reached(w.engine, w.clock, actor=actor)
    with pytest.raises(season.SeasonError) as info:
        season.set_frozen(w.engine, w.clock, actor, False)
    assert info.value.code == "goal_reached"


def test_new_season_carries_balances_and_restarts_pnl(mid_season: dict[str, Any]) -> None:
    w = mid_season["world"]
    p1, p2 = mid_season["p1"], mid_season["p2"]
    actor = admin_actor(w)
    with pytest.raises(season.SeasonError) as info:
        season.new_season(w.engine, w.clock, actor, start_x10=2194, goal_x10=2100)
    assert info.value.code == "not_frozen"
    season.set_frozen(w.engine, w.clock, actor, True)
    with pytest.raises(season.SeasonError) as info:  # locked markets and open pools remain
        season.new_season(w.engine, w.clock, actor, start_x10=2194, goal_x10=2100)
    assert info.value.code == "unsettled"
    season.set_frozen(w.engine, w.clock, actor, False)
    to_goal_day(w)
    season.goal_reached(w.engine, w.clock)
    ending = {p1: account(w, p1)[0], p2: account(w, p2)[0]}
    with pytest.raises(season.SeasonError):
        season.new_season(w.engine, w.clock, actor, start_x10=2194, goal_x10=2194)
    new_id = season.new_season(w.engine, w.clock, actor, start_x10=2194, goal_x10=2100)
    with w.engine.connect() as conn:
        seasons = conn.execute(select(Season.id, Season.status, Season.ended_at)).all()
        new = conn.execute(select(Season).where(Season.id == new_id)).one()
        accounts = {
            user: (bal, pnl)
            for user, bal, pnl in conn.execute(
                select(Account.user_id, Account.balance_cents, Account.pnl_cents).where(
                    Account.season_id == new_id, Account.kind == "player"
                )
            )
        }
        kinds = set(
            conn.execute(
                select(LedgerEntry.kind)
                .join(Account, Account.id == LedgerEntry.account_id)
                .where(Account.season_id == new_id)
            ).scalars()
        )
        assert instance.current_state(conn) == instance.ACTIVE
        assert ledger.verify(conn).ok
    assert sorted(s for _, s, _ in seasons) == ["active", "ended"]
    assert (new.number, new.direction, new.goal_weight_x10) == (2, "down", 2100)
    assert accounts == {p1: (ending[p1], 0), p2: (ending[p2], 0)}
    assert kinds == {"season_carry"}
    # A player who joins now gets the starting grant in the new season.
    code = auth.rotate_registration_code(w.engine, SystemClock())
    late = auth.register(
        w.engine,
        SystemClock(),
        email="late@example.invalid",
        display_name="Late",
        password=web.PASSWORD,
        code=code,
        ip="10.0.0.9",
    )
    with w.engine.connect() as conn:
        bal = conn.execute(
            select(Account.balance_cents).where(
                Account.user_id == late, Account.season_id == new_id
            )
        ).scalar_one()
    assert bal == 100_000
    with pytest.raises(season.SeasonError):  # not frozen any more
        season.new_season(w.engine, w.clock, actor, start_x10=2100, goal_x10=2000)


def test_frozen_middleware_blocks_money_routes_but_not_history(world: World) -> None:
    w = world
    c = web.client(create_app(w.settings, domain_clock=w.clock))
    with c:
        code = auth.rotate_registration_code(w.engine, SystemClock())
        web.register(c, code)
        with immediate(w.engine) as conn:
            instance.set_instance_state(conn, w.clock, instance.FROZEN)
        token = web.page_csrf(c, "/")
        for path in ("/api/bets", "/api/bets/parlay", "/api/pools/1/enter"):
            r = c.post(path, json={}, headers={"X-CSRF-Token": token})
            assert r.status_code == 423 and r.json()["reason"] == "instance_frozen", path
        for page in ("/", "/bets/mine", "/bets/feed", "/leaderboard", "/stats"):
            r = c.get(page)
            assert r.status_code == 200, page
            assert 'data-testid="frozen-banner"' in r.text


def test_goal_watch_stands_down_without_a_goal(world: World) -> None:
    w = world  # the world's season has no goal set
    job = GoalWatchJob()
    ctx = JobContext(w.engine, w.clock, w.clock.now(), "tick")
    assert job.run(ctx) == 0 and job.idle
    set_goal(w, goal_x10=1500)  # set later: a rebuilt job (fresh instance) picks it up
    to_goal_day(w)
    fresh = GoalWatchJob()
    assert fresh.run(JobContext(w.engine, w.clock, w.clock.now(), "tick")) == 0
    assert not fresh.idle and fresh.scanned[1] is not None
