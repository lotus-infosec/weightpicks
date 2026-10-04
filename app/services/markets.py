"""Market drops and locks (BUILD_PLAN §1.4.3).

A drop prices its markets from observations *before* taking the write lock, then
inserts market + selections + odds version 1 for every new market in one short
BEGIN IMMEDIATE transaction. `markets.dedupe_key` is unique and re-checked under the
lock, so a repeated, overlapping or restarted drop never duplicates a market. A
market whose lock time has already passed is never opened, and a drop caught up after
its local day has ended posts nothing: it would be priced on data older than what
bettors can already see (D-030).
"""

from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import Connection, Engine, exists, func, insert, select, update

from app.core.clock import Clock
from app.core.db import immediate
from app.domain.lines import COUNT_WINDOW_DAYS, SIGMA_WINDOW_DAYS, Pricing
from app.domain.markets import (
    TEMPLATES,
    MarketSpec,
    MarketStatus,
    PricingData,
    Timeframe,
    drop_specs,
    transition,
)
from app.domain.schedule import local_date
from app.models import InstanceSettingsRow, Market, Observation, OddsVersion, Selection
from app.providers.base import COUNT_METRICS, WORKOUT_MIN_MINUTES
from app.services import instance
from app.services.instance import InstanceConfig
from app.services.ledger import active_season_id
from app.services.observations import canonical_weigh_ins, latest_complete_through
from app.services.outbox import Category, enqueue

log = structlog.get_logger()


@dataclass(slots=True)
class DropResult:
    timeframe: Timeframe
    day: date
    created: list[int] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)  # dedupe_key -> reason


# ---- pricing inputs ------------------------------------------------------------------


def pricing_data(conn: Connection, day: date) -> PricingData:
    """Canonical weigh-ins for 12 weeks, daily totals and workouts for 4 weeks, to `day`."""
    weigh_ins = {
        w.local_date: w.value
        for w in canonical_weigh_ins(conn, day - timedelta(days=SIGMA_WINDOW_DAYS - 1), day)
    }
    since = day - timedelta(days=COUNT_WINDOW_DAYS)
    latest = (
        func.row_number()
        .over(
            partition_by=(Observation.metric, Observation.local_date),
            order_by=(Observation.observed_at.desc(), Observation.id.desc()),
        )
        .label("rank")
    )
    ranked = (
        select(Observation.metric, Observation.local_date, Observation.value, latest)
        .where(Observation.metric.in_(COUNT_METRICS), Observation.local_date.between(since, day))
        .subquery()
    )
    totals: dict[str, dict[date, int]] = defaultdict(dict)
    for metric, local_day, value in conn.execute(
        select(ranked.c.metric, ranked.c.local_date, ranked.c.value).where(ranked.c.rank == 1)
    ):
        totals[metric][local_day] = value
    # A day with any daily total but no 10+ minute activity is a real zero-workout day.
    workouts = dict.fromkeys({d for per_day in totals.values() for d in per_day}, 0)
    for local_day, count in conn.execute(
        select(Observation.local_date, func.count())
        .where(
            Observation.metric == "activity",
            Observation.value >= WORKOUT_MIN_MINUTES,
            Observation.local_date.between(since, day),
        )
        .group_by(Observation.local_date)
    ):
        workouts[local_day] = count
    if workouts:
        totals["workouts"] = workouts
    return PricingData(
        as_of=day,
        weigh_ins=weigh_ins,
        daily_totals=dict(totals),
        complete_through=latest_complete_through(conn),
    )


def odds_json(pricing: Pricing, sides: Sequence[str]) -> dict[str, int | None]:
    return dict(zip(sides, (pricing.odds_over, pricing.odds_under), strict=True))


# ---- writes ----------------------------------------------------------------------------


def insert_market(
    conn: Connection,
    *,
    season_id: int,
    spec: MarketSpec,
    pricing: Pricing,
    now: datetime,
    origin: str = "core",
) -> int:
    """Insert an open market with its two selections and odds version 1."""
    status = transition(MarketStatus.DRAFT, MarketStatus.OPEN)
    market_id = conn.execute(
        insert(Market)
        .values(
            season_id=season_id,
            template=spec.template,
            timeframe=spec.timeframe.value,
            metric=spec.metric,
            window_start=spec.window_start,
            window_end=spec.window_end,
            params=spec.params,
            title=spec.title,
            status=status.value,
            origin=origin,
            opens_at=now,
            lock_at=spec.lock_at,
            settle_after=spec.settle_after,
            settle_deadline=spec.settle_deadline,
            correlation_keys=list(spec.correlation_keys),
            dedupe_key=spec.dedupe_key,
            created_at=now,
            status_changed_at=now,
        )
        .returning(Market.id)
    ).scalar_one()
    sides = spec.sides
    conn.execute(insert(Selection), [{"market_id": market_id, "side": s} for s in sides])
    inputs: dict[str, Any] = pricing.model_inputs | {
        "p_over": pricing.p_over,
        "q_over": pricing.q_over,
        "q_under": pricing.q_under,
    }
    conn.execute(
        insert(OddsVersion).values(
            market_id=market_id,
            version=1,
            line_x10=pricing.line_x10,
            odds=odds_json(pricing, sides),
            model_inputs=inputs,
            is_current=True,
            created_at=now,
        )
    )
    return market_id


def _blocked(conn: Connection) -> str | None:
    """Why no market may be posted right now, or None."""
    if instance.current_state(conn) == instance.FROZEN:
        return "frozen"
    if active_season_id(conn) is None:
        return "no_season"
    return None


def drop(
    engine: Engine,
    clock: Clock,
    config: InstanceConfig,
    timeframe: Timeframe,
    day: date,
    *,
    hold: float | None = None,
) -> DropResult:
    """Post the core markets of one drop (daily/weekly/monthly) for local date `day`."""
    result = DropResult(timeframe, day)
    specs = drop_specs(
        timeframe,
        day,
        schedule=config.schedule,
        tz=config.tz,
        unit=config.unit,
        enabled_metrics=config.enabled_metrics,
    )
    vig = float(config.economy.hold) if hold is None else hold  # settings, D-036
    now = clock.now()
    if local_date(now, config.tz) != day:
        result.skipped = dict.fromkeys((s.dedupe_key for s in specs), "stale_drop")
        log.info("market_drop_stale", timeframe=timeframe.value, day=day.isoformat())
        return result
    with engine.connect() as conn:  # read phase: no write lock while pricing
        blocked = _blocked(conn)
        if blocked:
            result.skipped = dict.fromkeys((s.dedupe_key for s in specs), blocked)
            return result
        data = pricing_data(conn, day)
    priced: list[tuple[MarketSpec, Pricing]] = []
    for spec in specs:
        if spec.lock_at <= now:
            result.skipped[spec.dedupe_key] = "lock_passed"
            continue
        pricing = TEMPLATES[spec.template].price(spec, data, config.unit, vig)
        if pricing is None:
            result.skipped[spec.dedupe_key] = "no_history"
            continue
        if (
            spec.metric == "weight"
            and timeframe is not Timeframe.DAILY
            and pricing.model_inputs.get("provisional")
        ):
            # A provisional prior (slope 0) over 7-28 days makes "under" nearly free for
            # a subject who is losing weight; only the daily line is offered (D-039).
            result.skipped[spec.dedupe_key] = "provisional"
            continue
        priced.append((spec, pricing))

    with immediate(engine) as conn:
        blocked = _blocked(conn)  # re-checked under the write lock
        season_id = active_season_id(conn)
        if blocked or season_id is None:
            reason = blocked or "no_season"
            result.skipped |= dict.fromkeys((s.dedupe_key for s, _ in priced), reason)
            return result
        keys = [s.dedupe_key for s, _ in priced]
        existing = set(
            conn.execute(select(Market.dedupe_key).where(Market.dedupe_key.in_(keys))).scalars()
        )
        announced: list[dict[str, object]] = []
        for spec, pricing in priced:
            if spec.dedupe_key in existing:
                result.skipped[spec.dedupe_key] = "exists"
                continue
            market_id = insert_market(
                conn, season_id=season_id, spec=spec, pricing=pricing, now=now
            )
            result.created.append(market_id)
            announced.append(
                {
                    "market_id": market_id,
                    "title": spec.title,
                    "line_x10": pricing.line_x10,
                    "odds_over": pricing.odds_over,
                    "odds_under": pricing.odds_under,
                }
            )
        if announced:
            enqueue(
                conn,
                clock,
                category=Category.NEW_MARKETS,
                payload={
                    "timeframe": timeframe.value,
                    "day": day.isoformat(),
                    "markets": announced,
                },
                dedupe_key=f"new_markets:{timeframe.value}:{day.isoformat()}",
            )
    log.info(
        "market_drop",
        timeframe=timeframe.value,
        day=day.isoformat(),
        created=len(result.created),
        skipped=dict(sorted(Counter(result.skipped.values()).items())),
    )
    return result


def set_status(
    conn: Connection,
    market_ids: Sequence[int],
    current: MarketStatus,
    target: MarketStatus,
    now: datetime,
) -> int:
    """Move markets from `current` to `target`; the state machine decides if that's legal."""
    new = transition(current, target)
    if not market_ids:
        return 0
    result = conn.execute(
        update(Market)
        .where(Market.id.in_(market_ids), Market.status == current.value)
        .values(status=new.value, status_changed_at=now)
    )
    return result.rowcount


def lock_open_markets(conn: Connection, now: datetime, *, everything: bool = False) -> int:
    """Lock open markets whose lock time has come (or all of them: instance frozen)."""
    query = select(Market.id).where(Market.status == MarketStatus.OPEN.value)
    if not everything:
        query = query.where(Market.lock_at <= now)
    ids = list(conn.execute(query).scalars())
    count = set_status(conn, ids, MarketStatus.OPEN, MarketStatus.LOCKED, now)
    if count:
        log.info("markets_locked", count=count, frozen=everything)
    return count


@dataclass(frozen=True, slots=True)
class LockPass:
    locked: int
    next_lock_at: datetime | None  # earliest lock time still pending (None if none open)


def lock_due(engine: Engine, now: datetime) -> LockPass:
    """Every-tick lock pass: one read; takes the write lock only when something is due."""
    open_ = Market.status == MarketStatus.OPEN.value
    state = (
        select(InstanceSettingsRow.instance_state)
        .where(InstanceSettingsRow.id == 1)
        .scalar_subquery()
    )
    with engine.connect() as conn:
        frozen_state, earliest, any_open = conn.execute(
            select(
                state,
                select(func.min(Market.lock_at)).where(open_).scalar_subquery(),
                select(exists().where(open_)).scalar_subquery(),
            )
        ).one()
    frozen = frozen_state == instance.FROZEN
    if not any_open or (not frozen and earliest > now):
        return LockPass(0, earliest)
    with immediate(engine) as conn:
        frozen = instance.current_state(conn) == instance.FROZEN
        locked = lock_open_markets(conn, now, everything=frozen)
        pending = conn.execute(select(func.min(Market.lock_at)).where(open_)).scalar_one()
    return LockPass(locked, pending)
