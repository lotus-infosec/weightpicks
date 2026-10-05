"""Price Is Right pools end to end (D-010, D-043): create, enter, lock, settle, refund."""

from datetime import date, timedelta

import pytest
from sqlalchemy import select

from app.core.db import immediate
from app.models import Account, AuditEntry, Bust, OutboxMessage, Pool, PoolEntry
from app.services import busts, instance, ledger, pools
from tests.integration.test_bets_settlement import account, player, series
from tests.integration.world import World, local, sync_sim

TARGET = date(2026, 10, 12)  # a week after the world's Mon Oct 5


def config(w: World) -> instance.InstanceConfig:
    with w.engine.connect() as conn:
        c = instance.read(conn)
    assert c is not None
    return c


def draft(w: World, **over: object) -> pools.Draft:
    raw = {
        "title": "Columbus Day pot",
        "question": "What will the scale say on Monday Oct 12?",
        "target_date": TARGET.isoformat(),
        "buy_in_cents": 5_000,
    } | over
    return pools.validate(config(w), w.clock.now(), raw)


def make(w: World, **over: object) -> int:
    return pools.create(w.engine, w.clock, draft(w, **over), actor_id=None)


def escrow(w: World, pool_id: int) -> int:
    with w.engine.connect() as conn:
        return int(
            conn.execute(
                select(Account.balance_cents)
                .join(Pool, Pool.account_id == Account.id)
                .where(Pool.id == pool_id)
            ).scalar_one()
        )


def status(w: World, pool_id: int) -> tuple[str, int | None, dict[str, object] | None]:
    with w.engine.connect() as conn:
        row = conn.execute(
            select(Pool.status, Pool.result_x10, Pool.outcome).where(Pool.id == pool_id)
        ).one()
    return row.status, row.result_x10, row.outcome


def after_target(w: World, day: date = TARGET) -> None:
    w.clock.set(local(day.year, day.month, day.day, 13, 30))
    sync_sim(w.engine, w.clock)


def test_validation_rules(world: World) -> None:
    w = world
    d = draft(w)
    assert d.lock_at == local(2026, 10, 11, 22)  # the night before, at the bet lock
    assert d.settle_after == local(2026, 10, 12, 11)  # after the weigh-in window
    assert d.notes == ()
    big = draft(w, buy_in_cents=99_999_999)
    assert big.buy_in_cents == 50_000 and "adjusted" in big.notes[0]
    assert draft(w, buy_in_cents=None).buy_in_cents == 10_000  # the economy default
    assert draft(w, buy_in_cents=1).buy_in_cents == 100
    bad = {
        "target_date": [(date(2026, 10, 6)).isoformat(), (date(2027, 3, 1)).isoformat(), "soon"],
        "title": ["", "x" * 81],
        "question": [""],
        "text": ["Click https://evil.example", "Hey @everyone"],
    }
    for code, values in bad.items():
        for value in values:
            key = "title" if code == "text" else code
            with pytest.raises(pools.PoolError) as info:
                draft(w, **{key: value})
            assert info.value.code == code, (code, value)
    with pytest.raises(pools.PoolError, match="bettor name"):
        pools.validate(
            config(w), w.clock.now(), draft(w).as_json() | {"title": "Beat P1"}, names=["P1"]
        )
    with pytest.raises(pools.PoolError, match="buy-in"):
        draft(w, buy_in_cents="lots")


def test_create_enter_change_guess_and_lock(world: World) -> None:
    w = world
    p1, p2 = player(w, 1), player(w, 2)
    pool_id = make(w)
    with w.engine.connect() as conn:
        posted = conn.execute(select(OutboxMessage.dedupe_key)).scalars().all()
        actions = conn.execute(select(AuditEntry.action)).scalars().all()
    assert f"pool_open:{pool_id}" in posted and "pool.create" in actions
    assert pools.enter(w.engine, w.clock, user_id=p1, pool_id=pool_id, guess_x10=2150) is True
    assert pools.enter(w.engine, w.clock, user_id=p1, pool_id=pool_id, guess_x10=2140) is False
    assert pools.enter(w.engine, w.clock, user_id=p2, pool_id=pool_id, guess_x10=2160) is True
    assert account(w, p1) == (95_000, -5_000)  # one buy-in, guess changes are free
    assert escrow(w, pool_id) == 10_000
    with w.engine.connect() as conn:
        guesses = dict(conn.execute(select(PoolEntry.user_id, PoolEntry.guess_x10)).all())
    assert guesses == {p1: 2140, p2: 2160}
    with pytest.raises(pools.PoolError) as info:
        pools.enter(w.engine, w.clock, user_id=p1, pool_id=pool_id, guess_x10=10)
    assert info.value.code == "guess"
    with pytest.raises(pools.PoolError) as info:
        pools.enter(w.engine, w.clock, user_id=p1, pool_id=999, guess_x10=2150)
    assert info.value.code == "not_found"
    w.clock.set(local(2026, 10, 11, 22, 1))
    with pytest.raises(pools.PoolError) as info:
        pools.enter(w.engine, w.clock, user_id=p1, pool_id=pool_id, guess_x10=2100)
    assert info.value.code == "locked"
    assert pools.tick(w.engine, w.clock).locked == 1
    assert status(w, pool_id)[0] == "locked"


def test_refusals(world: World) -> None:
    w = world
    p1, broke = player(w, 1), player(w, 2, grant=2_000)
    pool_id = make(w)
    with pytest.raises(pools.PoolError) as info:
        pools.enter(w.engine, w.clock, user_id=broke, pool_id=pool_id, guess_x10=2150)
    assert info.value.code == "insufficient_funds"
    with w.engine.connect() as conn:
        assert conn.execute(select(PoolEntry.id)).first() is None  # rolled back with it
    assert pools.parse_guess("212.4") == 2124
    for raw in ("abc", "2", "99999"):
        with pytest.raises(pools.PoolError):
            pools.parse_guess(raw)
    with immediate(w.engine) as conn:
        instance.set_instance_state(conn, w.clock, instance.FROZEN)
    with pytest.raises(pools.PoolError) as info:
        pools.enter(w.engine, w.clock, user_id=p1, pool_id=pool_id, guess_x10=2150)
    assert info.value.code == "frozen"
    with pytest.raises(pools.PoolError) as info:
        make(w, title="Another")
    assert info.value.code == "frozen"


def test_settles_closest_without_going_over_after_the_data_is_complete(world: World) -> None:
    w = world
    actual = series()[TARGET]
    users = [player(w, n) for n in range(1, 5)]
    pool_id = make(w, buy_in_cents=3_333)
    guesses = [actual + 1, actual - 3, actual - 3, actual - 20]  # over, tie, tie, far
    for user, guess in zip(users, guesses, strict=True):
        pools.enter(w.engine, w.clock, user_id=user, pool_id=pool_id, guess_x10=guess)
    # Before the target day's data is complete: nothing settles.
    w.clock.set(local(2026, 10, 12, 9))
    sync_sim(w.engine, w.clock)
    assert pools.tick(w.engine, w.clock).finished == 0
    assert status(w, pool_id)[0] == "locked"
    after_target(w)
    assert pools.tick(w.engine, w.clock).finished == 1
    state, result, outcome = status(w, pool_id)
    assert (state, result) == ("settled", actual)
    assert outcome == {"reason": None, "winners": users[1:3], "entries": 4}
    pot = 4 * 3_333
    assert account(w, users[1])[0] == 100_000 - 3_333 + pot // 2 + pot % 2  # earliest tie
    assert account(w, users[2])[0] == 100_000 - 3_333 + pot // 2
    assert account(w, users[0]) == (100_000 - 3_333, -3_333)
    assert escrow(w, pool_id) == 0
    with w.engine.connect() as conn:
        assert ledger.verify(conn).ok
        posted = conn.execute(
            select(OutboxMessage.payload).where(
                OutboxMessage.dedupe_key == f"pool_result:{pool_id}"
            )
        ).scalar_one()
    assert posted["winners"] == users[1:3] and posted["pot_cents"] == pot
    assert pools.settle_pool(w.engine, w.clock, pool_id) is None  # rerun: no-op


def test_everyone_over_or_nobody_in_is_refunded(world: World) -> None:
    w = world
    actual = series()[TARGET]
    p1, p2 = player(w, 1), player(w, 2)
    over = make(w)
    empty = make(w, title="Empty pot")
    pools.enter(w.engine, w.clock, user_id=p1, pool_id=over, guess_x10=actual + 1)
    pools.enter(w.engine, w.clock, user_id=p2, pool_id=over, guess_x10=actual + 50)
    after_target(w)
    pools.tick(w.engine, w.clock)
    assert status(w, over)[0] == "refunded" and status(w, over)[2]["reason"] == "nobody_eligible"  # type: ignore[index]
    assert status(w, empty)[2]["reason"] == "no_entries"  # type: ignore[index]
    assert account(w, p1) == (100_000, 0)  # refund nets P&L to zero
    assert escrow(w, over) == 0


def test_admin_refund_and_a_pot_entry_blocks_a_bust(world: World) -> None:
    w = world
    p1 = player(w, 1, grant=5_000)
    pool_id = make(w, buy_in_cents=5_000)
    pools.enter(w.engine, w.clock, user_id=p1, pool_id=pool_id, guess_x10=2100)
    assert account(w, p1)[0] == 0
    with immediate(w.engine) as conn:
        season = ledger.active_season_id(conn)
        assert season is not None
        assert busts.check(conn, w.clock, season) == []  # money is in the pot
        assert pools.refund_pool(conn, w.clock, pool_id, "admin_void") is True
        assert pools.refund_pool(conn, w.clock, pool_id, "admin_void") is False
    assert account(w, p1) == (5_000, 0)
    assert status(w, pool_id)[0] == "refunded"
    with w.engine.connect() as conn:
        assert conn.execute(select(Bust.id)).first() is None


def test_too_late_to_open(world: World) -> None:
    w = world
    d = draft(w, target_date=(date(2026, 10, 7)).isoformat())
    w.clock.set(d.lock_at + timedelta(minutes=1))
    with pytest.raises(pools.PoolError) as info:
        pools.create(w.engine, w.clock, d, actor_id=None)
    assert info.value.code == "late"
