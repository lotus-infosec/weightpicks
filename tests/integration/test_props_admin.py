"""Admin-created props (D-041) and early settlement through the real pipeline."""

from datetime import timedelta

import pytest
from sqlalchemy import select, update

from app.core.clock import SystemClock
from app.core.db import immediate
from app.models import AuditEntry, InstanceSettingsRow, Market, OutboxMessage, Selection, Settlement
from app.services import auth, instance, props, settlement
from app.services.admin import Actor
from app.services.observations import canonical_weigh_ins
from tests.integration.test_bets_settlement import series
from tests.integration.world import NY, World, local, sync_sim


def enable(w: World, on: bool = True) -> None:
    with immediate(w.engine) as conn:
        flags = conn.execute(select(InstanceSettingsRow.flags)).scalar_one() or {}
        conn.execute(update(InstanceSettingsRow).values(flags=dict(flags) | {"props_futures": on}))


@pytest.fixture(autouse=True)
def _admin(world: World) -> None:
    global ACTOR
    admin_id = auth.create_admin(
        world.engine, SystemClock(), email="a@example.invalid", display_name="A", password="x" * 12
    )
    ACTOR = Actor(user_id=admin_id)


ACTOR = Actor(user_id=0)


def latest_weight(w: World) -> float:
    today = w.clock.now().astimezone(NY).date()
    with w.engine.connect() as conn:
        return canonical_weigh_ins(conn, today - timedelta(days=5), today)[-1].value / 10


def milestone_form(w: World, below: float, days: int = 10) -> dict[str, str]:
    deadline = w.clock.now().astimezone(NY).date() + timedelta(days=days)
    return {"threshold": f"{latest_weight(w) - below:.1f}", "deadline": deadline.isoformat()}


def priced_milestone(w: World, days: int = 10) -> dict[str, str]:
    """A milestone the engine will price (neither near-certain nor hopeless)."""
    today = w.clock.now().astimezone(NY).date()
    with w.engine.connect() as conn:
        config = instance.read(conn)
        assert config is not None
        for tenths in range(5, 120, 5):
            form = milestone_form(w, tenths / 10, days)
            try:
                p = props.preview(conn, config, today, "milestone_by", form)
            except props.PropError:
                continue
            if 0.3 < p.pricing.p_over < 0.8:
                return form
    raise AssertionError("no priceable milestone")


def test_create_preview_and_refusals(world: World) -> None:
    w = world
    with pytest.raises(props.PropError, match="turned off"):
        props.create(w.engine, w.clock, ACTOR, "milestone_by", milestone_form(w, 1.0))
    enable(w)
    form = priced_milestone(w)
    market_id = props.create(w.engine, w.clock, ACTOR, "milestone_by", form)
    with w.engine.connect() as conn:
        m = conn.execute(select(Market).where(Market.id == market_id)).one()
        sides = (
            conn.execute(select(Selection.side).where(Selection.market_id == market_id))
            .scalars()
            .all()
        )
        announced = conn.execute(
            select(OutboxMessage.payload).where(
                OutboxMessage.dedupe_key == f"new_markets:admin:{market_id}"
            )
        ).scalar_one()
        audited = conn.execute(select(AuditEntry.action)).scalars().all()
    assert (m.origin, m.timeframe, m.status) == ("admin", "prop", "open")
    assert sorted(sides) == ["no", "yes"]
    assert m.lock_at == local(2026, 10, 5, 22)  # locks the creation night
    assert announced["markets"][0]["title"] == m.title
    assert "market.create_prop" in audited
    with pytest.raises(props.PropError, match="already exists"):
        props.create(w.engine, w.clock, ACTOR, "milestone_by", form)
    with pytest.raises(props.PropError, match="won't price"):
        props.create(w.engine, w.clock, ACTOR, "milestone_by", milestone_form(w, -5.0))  # reached
    with pytest.raises(props.PropError, match="valid value"):
        props.create(w.engine, w.clock, ACTOR, "milestone_by", {"threshold": "x"})
    w.clock.set(local(2026, 10, 5, 22, 30))
    with pytest.raises(props.PropError, match="Too late"):
        props.create(w.engine, w.clock, ACTOR, "milestone_by", priced_milestone(w, days=9))


def test_every_template_can_be_created(world: World) -> None:
    w = world
    enable(w)
    today = w.clock.now().astimezone(NY).date()
    with w.engine.connect() as conn:
        current, _ = props._streak(conn, today, "weigh_in")
    made = [
        props.create(
            w.engine,
            w.clock,
            ACTOR,
            "streak_reaches",
            {
                "kind": "weigh_in",
                "n": str(current + 3),
                "deadline": (today + timedelta(days=10)).isoformat(),
            },
        ),
        props.create(w.engine, w.clock, ACTOR, "beat_last_week", {"metric": "steps"}),
        props.create(
            w.engine,
            w.clock,
            ACTOR,
            "future_total_change",
            {"day": (today + timedelta(days=30)).isoformat()},
        ),
    ]
    with w.engine.connect() as conn:
        rows = conn.execute(
            select(Market.template, Market.timeframe).where(Market.id.in_(made))
        ).all()
    assert sorted(rows) == [
        ("beat_last_week", "prop"),
        ("future_total_change", "future"),
        ("streak_reaches", "prop"),
    ]


def test_milestone_settles_as_soon_as_it_is_reached(world: World) -> None:
    w = world
    enable(w)
    # A threshold the simulator's own series reaches a few days in, among ones the engine
    # will price: the market must settle Yes as soon as that weigh-in syncs.
    known = series()
    today = w.clock.now().astimezone(NY).date()
    market_id = None
    for k in range(2, 9):  # reached by day k; the deadline is two days later
        deadline = today + timedelta(days=k + 2)
        reached = min(v for d, v in known.items() if today < d <= today + timedelta(days=k))
        form = {"threshold": f"{reached / 10:.1f}", "deadline": deadline.isoformat()}
        try:
            market_id = props.create(w.engine, w.clock, ACTOR, "milestone_by", form)
            break
        except props.PropError:
            continue
    assert market_id is not None
    with w.engine.connect() as conn:
        settle_after = conn.execute(
            select(Market.settle_after).where(Market.id == market_id)
        ).scalar_one()
    from app.services import markets

    settled_at = None
    for day in range(1, 15):
        w.clock.set(
            local(2026, 10, 5 + day, 13, 30) if 5 + day <= 31 else local(2026, 11, day - 26, 13, 30)
        )
        sync_sim(w.engine, w.clock)
        markets.lock_due(w.engine, w.clock.now())
        settlement.settle_due(w.engine, w.clock)
        with w.engine.connect() as conn:
            outcome = conn.execute(
                select(Settlement.outcome).where(Settlement.market_id == market_id)
            ).scalar_one_or_none()
        if outcome:
            settled_at = w.clock.now()
            break
    assert outcome is not None and outcome["winner"] == "yes", outcome
    assert settled_at is not None and settled_at < settle_after  # early, before the deadline
    with w.engine.connect() as conn:
        assert instance.read(conn) is not None
