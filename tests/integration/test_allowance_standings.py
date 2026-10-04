from datetime import date, timedelta

from sqlalchemy import func, select, update

from app.core.db import immediate
from app.domain.ledger import EntryKind
from app.models import Bust, InstanceSettingsRow, LedgerEntry, OutboxMessage, User
from app.services import allowance, busts, instance, standings
from app.services.ledger import active_season_id
from app.worker.jobs.economy import DailyAllowanceJob, WeeklyStandingsJob
from app.worker.registry import run_due
from tests.integration.test_bets_settlement import account, bet, player, weight_market
from tests.integration.world import NY, World, local, mark_setup_done

ALLOWANCE = 5_000  # default $50


def allowances(w: World) -> int:
    with w.engine.connect() as conn:
        return conn.execute(
            select(func.count()).where(
                LedgerEntry.kind == EntryKind.ALLOWANCE.value, LedgerEntry.amount_cents > 0
            )
        ).scalar_one()


def test_pays_every_non_banned_player_once_a_day_without_touching_pnl(world: World) -> None:
    w = world
    mark_setup_done(w.engine, w.settings)
    a, b, banned = player(w, 1), player(w, 2), player(w, 3)
    with immediate(w.engine) as conn:
        conn.execute(update(User).where(User.id == b).values(status="frozen"))
        conn.execute(update(User).where(User.id == banned).values(status="banned"))
    w.clock.advance(timedelta(days=1))  # joined on Oct 5; first allowance is Oct 6
    day = w.clock.now().astimezone(NY).date()
    assert allowance.pay_due(w.engine, w.clock, NY, day) == 2
    assert allowance.pay_due(w.engine, w.clock, NY, day) == 0  # idempotent
    assert account(w, a) == (100_000 + ALLOWANCE, 0)  # (balance, P&L)
    assert account(w, b) == (100_000 + ALLOWANCE, 0)  # frozen players still get it
    assert account(w, banned) == (100_000, 0)


def test_catch_up_after_downtime(world: World) -> None:
    w = world
    mark_setup_done(w.engine, w.settings)
    user = player(w, 1)
    w.clock.advance(timedelta(days=4))
    job = DailyAllowanceJob(instance.read(w.engine.connect()))  # type: ignore[arg-type]
    w.clock.advance(timedelta(hours=-11, minutes=-50))  # 00:10 local on Oct 9
    assert run_due([job], w.engine, w.clock) == ["daily_allowance"]
    assert account(w, user)[0] == 100_000 + 4 * ALLOWANCE  # Oct 6, 7, 8, 9
    assert run_due([job], w.engine, w.clock) == []


def test_nothing_is_paid_when_frozen_or_before_setup(world: World) -> None:
    w = world
    player(w, 1)
    w.clock.advance(timedelta(days=2))
    today = w.clock.now().astimezone(NY).date()
    with immediate(w.engine) as conn:
        conn.execute(update(InstanceSettingsRow).values(setup_completed_at=None))
    assert allowance.pay_due(w.engine, w.clock, NY, today) == 0  # setup not finished
    mark_setup_done(w.engine, w.settings)
    with immediate(w.engine) as conn:
        conn.execute(update(InstanceSettingsRow).values(instance_state=instance.FROZEN))
    assert allowance.pay_due(w.engine, w.clock, NY, today) == 0
    assert allowances(w) == 0


def test_allowance_resolves_a_bust(world: World) -> None:
    w = world
    mark_setup_done(w.engine, w.settings)
    user = player(w, 1, grant=50)  # under $1: bust once checked
    with immediate(w.engine) as conn:
        busts.check(conn, w.clock, active_season_id(conn))  # type: ignore[arg-type]
    w.clock.advance(timedelta(days=1))
    allowance.pay_due(w.engine, w.clock, NY, w.clock.now().astimezone(NY).date())
    with w.engine.connect() as conn:
        resolved = conn.execute(select(Bust.resolved_at).where(Bust.user_id == user)).scalar_one()
    assert resolved is not None


def test_weekly_standings_post_once_with_last_weeks_pnl(world: World) -> None:
    w = world
    mark_setup_done(w.engine, w.settings)
    a, b = player(w, 1), player(w, 2)
    market = weight_market(w, -5)
    bet(w, a, market, "over", 1_000)
    config = instance.read(w.engine.connect())
    job = WeeklyStandingsJob(config)  # type: ignore[arg-type]
    w.clock.set(local(2026, 10, 12, 9, 30))  # Monday after
    assert run_due([job], w.engine, w.clock) == ["weekly_standings"]
    assert run_due([job], w.engine, w.clock) == []
    with w.engine.connect() as conn:
        (payload,) = conn.execute(
            select(OutboxMessage.payload).where(OutboxMessage.category == "weekly_standings")
        ).scalars()
    assert payload["week"] == "2026-W41"
    assert {r["user_id"] for r in payload["rows"]} == {a, b}
    assert next(r for r in payload["rows"] if r["user_id"] == a)["week_pnl_cents"] == -1_000


def test_standings_skip_when_days_late(world: World) -> None:
    w = world
    player(w, 1)
    job = WeeklyStandingsJob(instance.read(w.engine.connect()))  # type: ignore[arg-type]
    w.clock.set(local(2026, 10, 14, 12))  # Wednesday: Monday's post would be stale
    assert run_due([job], w.engine, w.clock) == ["weekly_standings"]
    assert standings.post_week(w.engine, w.clock, NY, date(2026, 10, 12)) is True  # direct call
    with w.engine.connect() as conn:
        n = conn.execute(
            select(func.count()).where(OutboxMessage.category == "weekly_standings")
        ).scalar_one()
    assert n == 1  # only the direct call queued one
