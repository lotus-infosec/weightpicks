from datetime import date, timedelta

import pytest
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.exc import IntegrityError, OperationalError

from app.core.clock import SystemClock
from app.core.db import immediate
from app.core.security import verify_password
from app.domain.economy import Economy
from app.models import (
    AuditEntry,
    BannedEmail,
    Bet,
    Bust,
    Command,
    InstanceSettingsRow,
    LedgerTxn,
    Market,
    OutboxMessage,
    Session,
    Settlement,
    User,
)
from app.services import admin, auth, busts, ledger, settlement
from app.services.admin import AdminError
from app.services.audit import Actor
from app.services.auth import AuthError
from app.worker.jobs.commands import CommandsJob
from app.worker.registry import JobContext
from tests.integration.test_bets_settlement import (
    account,
    bet,
    change,
    player,
    to_settle_time,
    weight_market,
)
from tests.integration.world import World, create_admin

PW = "correct horse battery"


@pytest.fixture
def boss(world: World) -> Actor:
    admin_id = create_admin(
        world.engine,
        SystemClock(),
        email="admin@example.invalid",
        display_name="Admin",
        password=PW,
    )
    return Actor(admin_id, "127.0.0.1")


def audits(w: World) -> list[tuple[str, str | None]]:
    with w.engine.connect() as conn:
        return [
            (a, r)
            for a, r in conn.execute(
                select(AuditEntry.action, AuditEntry.reason).order_by(AuditEntry.id)
            )
        ]


def refused(fn: object) -> str:
    with pytest.raises(AdminError) as exc:
        fn()  # type: ignore[operator]
    return exc.value.code


# ---- void --------------------------------------------------------------------------------


def test_void_refunds_every_open_stake_once(world: World, boss: Actor) -> None:
    market = weight_market(world, -5)
    a, b = player(world, 1), player(world, 2)
    bet(world, a, market, "over")
    bet(world, b, market, "under", stake=25_000)
    assert admin.void_market(world.engine, world.clock, boss, market, "scale broke") == 2
    assert account(world, a) == (100_000, 0) and account(world, b) == (100_000, 0)
    with world.engine.connect() as conn:
        txns = conn.execute(select(func.count()).select_from(LedgerTxn)).scalar_one()
        assert conn.execute(select(Market.status)).scalar_one() == "voided"
        assert set(conn.execute(select(Bet.status)).scalars()) == {"void"}
        cats = sorted(conn.execute(select(OutboxMessage.category)).scalars())
    assert cats.count("bet_results") == 2 and "market_settlements" in cats
    assert admin.void_market(world.engine, world.clock, boss, market, "again") == 0  # idempotent
    with world.engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(LedgerTxn)).scalar_one() == txns
        assert ledger.verify(conn).ok
    assert audits(world)[-1] == ("market.void", "scale broke")


def test_void_rules(world: World, boss: Actor) -> None:
    assert (
        refused(lambda: admin.void_market(world.engine, world.clock, boss, 999, "x")) == "not_found"
    )
    market = weight_market(world, change(date(2026, 10, 5), date(2026, 10, 6)) - 5)
    assert (
        refused(lambda: admin.void_market(world.engine, world.clock, boss, market, " "))
        == "reason_required"
    )
    to_settle_time(world)  # locks it
    settlement.settle_market(world.engine, world.clock, market)
    assert (
        refused(lambda: admin.void_market(world.engine, world.clock, boss, market, "late"))
        == "not_voidable"
    )


def test_voided_market_never_settles(world: World, boss: Actor) -> None:
    market = weight_market(world, -5)
    bet(world, player(world, 1), market, "over")
    to_settle_time(world)
    admin.void_market(world.engine, world.clock, boss, market, "bad line")
    assert settlement.settle_market(world.engine, world.clock, market).reason == "status_voided"
    with world.engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(Settlement)).scalar_one() == 0


# ---- users -----------------------------------------------------------------------------------


def test_freeze_unfreeze(world: World, boss: Actor) -> None:
    user = player(world, 1)
    admin.set_frozen(world.engine, world.clock, boss, user, True)
    admin.set_frozen(world.engine, world.clock, boss, user, True)  # no-op, no extra audit
    admin.set_frozen(world.engine, world.clock, boss, user, False)
    assert [a for a, _ in audits(world)] == ["user.freeze", "user.unfreeze"]
    assert (
        refused(lambda: admin.set_frozen(world.engine, world.clock, boss, boss.user_id, True))
        == "not_player"
    )


def test_ban_removes_the_player(world: World, boss: Actor) -> None:
    user = player(world, 1)
    market = weight_market(world, -5)
    bet(world, user, market, "over", stake=30_000)
    assert admin.ban(world.engine, world.clock, boss, user, "cheating") == 1
    assert account(world, user)[0] == 100_000  # refunded
    with world.engine.connect() as conn:
        assert conn.execute(select(User.status).where(User.id == user)).scalar_one() == "banned"
        assert conn.execute(select(BannedEmail.email)).scalars().all() == ["p1@example.invalid"]
        assert conn.execute(select(Bet.status)).scalar_one() == "void"
    code = auth.rotate_registration_code(world.engine, SystemClock())
    with pytest.raises(AuthError) as exc:
        auth.register(
            world.engine,
            SystemClock(),
            email="P1@example.invalid",
            display_name="Again",
            password=PW,
            code=code,
            ip="9.9.9.9",
        )
    assert exc.value.code == "banned"
    assert admin.ban(world.engine, world.clock, boss, user, "again") == 0
    assert (
        refused(lambda: admin.ban(world.engine, world.clock, boss, boss.user_id, "x"))
        == "not_player"
    )
    assert (
        refused(lambda: admin.ban(world.engine, world.clock, boss, user, "")) == "reason_required"
    )


def test_ban_ends_sessions_and_reset_password(world: World, boss: Actor) -> None:
    code = auth.rotate_registration_code(world.engine, SystemClock())
    user = auth.register(
        world.engine,
        SystemClock(),
        email="s@example.invalid",
        display_name="S",
        password=PW,
        code=code,
        ip="2.2.2.2",
    )
    auth.login(
        world.engine,
        SystemClock(),
        email="s@example.invalid",
        password=PW,
        ip="2.2.2.2",
        user_agent=None,
    )
    temporary = admin.reset_password(world.engine, world.clock, boss, user)
    with world.engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(Session)).scalar_one() == 0
        stored = conn.execute(select(User.password_hash).where(User.id == user)).scalar_one()
        audit_after = conn.execute(
            select(AuditEntry.after).where(AuditEntry.action == "user.reset_password")
        ).scalar_one()
    assert len(temporary) == 12 and verify_password(stored, temporary)
    assert temporary not in str(audit_after)
    auth.login(
        world.engine,
        SystemClock(),
        email="s@example.invalid",
        password=temporary,
        ip="2.2.2.2",
        user_agent=None,
    )
    admin.ban(world.engine, world.clock, boss, user, "bye")
    with world.engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(Session)).scalar_one() == 0


# ---- busts and bailouts ------------------------------------------------------------------------


def bust_player(w: World, n: int = 1) -> int:
    """A player loses everything on one settled market."""
    user = player(w, n, grant=10_000)
    d0, d1 = date(2026, 10, 5), date(2026, 10, 6)
    market = weight_market(w, change(d0, d1) - 5)  # Over wins
    bet(w, user, market, "under", stake=10_000)
    to_settle_time(w)
    assert settlement.settle_due(w.engine, w.clock).settled == 1
    return user


def test_losing_everything_creates_a_bust(world: World) -> None:
    user = bust_player(world)
    with world.engine.connect() as conn:
        rows = conn.execute(select(Bust.user_id, Bust.bailed_out_at)).all()
        season = ledger.active_season_id(conn)
        assert season is not None and busts.badge_counts(conn, season) == {user: 1}
        assert "busts" in conn.execute(select(OutboxMessage.category)).scalars().all()
    assert rows == [(user, None)]


def test_no_bust_while_a_bet_is_open(world: World) -> None:
    user = player(world, 1, grant=10_000)
    bet(world, user, weight_market(world, -5), "over", stake=10_000)
    with immediate(world.engine) as conn:
        season = ledger.active_season_id(conn)
        assert season is not None and busts.check(conn, world.clock, season) == []


def test_bailout_cooldown_edges(world: World, boss: Actor) -> None:
    user = bust_player(world)
    with world.engine.connect() as conn:
        busted_at = conn.execute(select(Bust.busted_at)).scalar_one()
    world.clock.set(busted_at + timedelta(hours=47, minutes=59))
    assert refused(lambda: admin.bailout(world.engine, world.clock, boss, user)) == "cooldown"
    world.clock.set(busted_at + timedelta(hours=48))
    assert admin.bailout(world.engine, world.clock, boss, user) == 50_000
    assert account(world, user) == (50_000, -10_000)  # bailouts never touch P&L
    assert (
        refused(lambda: admin.bailout(world.engine, world.clock, boss, user)) == "not_busted"
    )  # once per bust
    with world.engine.connect() as conn:
        assert conn.execute(select(Bust.bailed_out_at)).scalar_one() == world.clock.now()
        assert ledger.verify(conn).ok


def test_bailout_needs_a_bust_and_follows_the_economy(world: World, boss: Actor) -> None:
    solvent = player(world, 9)
    assert refused(lambda: admin.bailout(world.engine, world.clock, boss, solvent)) == "not_busted"
    with immediate(world.engine) as conn:
        conn.execute(
            update(InstanceSettingsRow).values(
                economy=Economy(bailout_cents=20_000, bailout_cooldown_days=0).to_json()
            )
        )
    user = bust_player(world)
    assert admin.bailout(world.engine, world.clock, boss, user) == 20_000


def test_recovery_by_adjustment_resolves_the_bust(world: World, boss: Actor) -> None:
    user = bust_player(world)
    admin.adjust(world.engine, world.clock, boss, user, 5_000, "goodwill")
    with world.engine.connect() as conn:
        assert conn.execute(select(Bust.resolved_at)).scalar_one() is not None
    assert refused(lambda: admin.bailout(world.engine, world.clock, boss, user)) == "not_busted"


def test_adjustments(world: World, boss: Actor) -> None:
    user = player(world, 1)
    admin.adjust(world.engine, world.clock, boss, user, -40_000, "typo in grant")
    assert account(world, user) == (60_000, 0)  # adjustments never touch P&L
    assert (
        refused(lambda: admin.adjust(world.engine, world.clock, boss, user, -60_001, "too far"))
        == "insufficient_funds"
    )
    assert (
        refused(lambda: admin.adjust(world.engine, world.clock, boss, user, 500, "  "))
        == "reason_required"
    )
    assert refused(lambda: admin.adjust(world.engine, world.clock, boss, user, 0, "x")) == "zero"
    assert audits(world)[-1] == ("bank.adjust", "typo in grant")


def test_economy_update_is_audited(world: World, boss: Actor) -> None:
    admin.update_economy(world.engine, world.clock, boss, Economy(daily_allowance_cents=2_500))
    admin.update_economy(
        world.engine, world.clock, boss, Economy(daily_allowance_cents=2_500)
    )  # no change
    with world.engine.connect() as conn:
        rows = conn.execute(
            select(AuditEntry.before, AuditEntry.after).where(
                AuditEntry.action == "settings.economy"
            )
        ).all()
    assert len(rows) == 1
    assert (
        rows[0][0]["daily_allowance_cents"] == 5_000
        and rows[0][1]["daily_allowance_cents"] == 2_500
    )


# ---- audit ---------------------------------------------------------------------------------------


def test_audit_is_append_only_and_redacted(world: World, boss: Actor) -> None:
    from app.services import audit

    with immediate(world.engine) as conn:
        audit.record(
            conn,
            world.clock,
            boss,
            action="test",
            after={"password": "hunter2", "nested": {"webhook_url": "https://x"}, "ok": 1},
        )
    with world.engine.connect() as conn:
        after = conn.execute(select(AuditEntry.after)).scalar_one()
    assert after == {"password": "[redacted]", "nested": {"webhook_url": "[redacted]"}, "ok": 1}
    for statement in ("UPDATE audit_log SET action = 'x'", "DELETE FROM audit_log"):
        with pytest.raises((IntegrityError, OperationalError)), immediate(world.engine) as conn:
            conn.execute(text(statement))


# ---- commands ------------------------------------------------------------------------------------


def test_sync_now_command(world: World, boss: Actor) -> None:
    dev = world.settings.model_copy(update={"data_provider": "simulated"})
    first = admin.request_sync(world.engine, world.clock, boss)
    assert admin.request_sync(world.engine, world.clock, boss) == first  # collapsed while pending
    job = CommandsJob(dev, world.clock)
    ctx = JobContext(world.engine, SystemClock(), SystemClock().now(), "tick")
    assert job.run(ctx) == 1
    assert job.run(ctx) == 0
    with world.engine.connect() as conn:
        status, result = conn.execute(select(Command.status, Command.result)).one()
    assert status == "done" and result["sync_status"] == "ok"
    with immediate(world.engine) as conn:
        conn.execute(
            insert(Command).values(
                type="explode", args={}, status="pending", created_at=SystemClock().now()
            )
        )
    assert job.run(ctx) == 1
    with world.engine.connect() as conn:
        assert (
            conn.execute(select(Command.status).where(Command.type == "explode")).scalar_one()
            == "failed"
        )


def test_audit_uses_the_real_time_the_admin_acted(world: World, boss: Actor) -> None:
    """In dev the domain clock is simulated; audit rows must still read in real time."""
    acted = SystemClock().now()
    user = player(world, 1)
    admin.set_frozen(world.engine, world.clock, Actor(boss.user_id, "1.2.3.4", acted), user, True)
    with world.engine.connect() as conn:
        ts, ip = conn.execute(select(AuditEntry.ts, AuditEntry.ip)).one()
    assert ts == acted and ts != world.clock.now() and ip == "1.2.3.4"
