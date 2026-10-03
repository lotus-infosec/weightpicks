"""Transactional settlement (BUILD_PLAN §1.4.2).

`settle_market` does everything in one BEGIN IMMEDIATE transaction: the readiness
gate, the `settlements` row (unique market_id = the idempotency guard), ledger
payouts/refunds, leg/bet statuses, the market's status and the outbox rows. A rerun
finds the row and does nothing; a crash part-way rolls everything back. Stale data
never auto-pushes: the market waits locked and one admin alert is queued once
`settle_deadline` passes (A15).
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import Connection, Engine, func, insert, select, update
from sqlalchemy.exc import IntegrityError

from app.core.clock import Clock
from app.core.db import immediate
from app.domain.markets import COMPLETENESS_KEY, TEMPLATES, MarketStatus, SettlementData
from app.domain.settlement import ENGINE_VERSION, Outcome, leg_result, readiness
from app.models import (
    Bet,
    BetLeg,
    Market,
    Observation,
    OddsVersion,
    Selection,
    Settlement,
    SyncRun,
)
from app.providers.base import COUNT_METRICS, WORKOUT_MIN_MINUTES
from app.services import ledger
from app.services.markets import set_status
from app.services.observations import canonical_weigh_ins, latest_complete_through
from app.services.outbox import Category, enqueue

log = structlog.get_logger()


@dataclass(frozen=True, slots=True)
class SettleResult:
    market_id: int
    settled: bool
    reason: str  # outcome reason when settled, else why it waited
    bets: int = 0


@dataclass(frozen=True, slots=True)
class SettlePass:
    settled: int
    alerts: int
    next_settle_after: datetime | None  # earliest settle_after of a locked market


def last_ok_sync_finished_at(conn: Connection) -> datetime | None:
    value: datetime | None = conn.execute(
        select(func.max(SyncRun.finished_at)).where(SyncRun.status == "ok")
    ).scalar_one()
    return value


def _days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=k) for k in range((end - start).days + 1)]


def settlement_data(
    conn: Connection, metric: str, start: date, end: date
) -> tuple[SettlementData, dict[str, Any]]:
    """Observations for one market, plus an audit record of exactly what was used."""
    if metric == "weight":
        canon = canonical_weigh_ins(conn, start, end)
        used = [c for c in canon if c.local_date in (start, end)]
        inputs: dict[str, Any] = {
            "weigh_ins": [
                {
                    "local_date": c.local_date.isoformat(),
                    "observation_id": c.observation_id,
                    "value": c.value,
                }
                for c in used
            ]
        }
        return SettlementData(weigh_ins={c.local_date: c.value for c in used}), inputs

    days = _days(start, end)
    rank = (
        func.row_number()
        .over(
            partition_by=(Observation.metric, Observation.local_date),
            order_by=(Observation.observed_at.desc(), Observation.id.desc()),
        )
        .label("rank")
    )
    latest = (
        select(Observation.id, Observation.metric, Observation.local_date, Observation.value, rank)
        .where(Observation.metric.in_(COUNT_METRICS), Observation.local_date.between(start, end))
        .subquery()
    )
    rows = conn.execute(
        select(latest.c.id, latest.c.metric, latest.c.local_date, latest.c.value).where(
            latest.c.rank == 1
        )
    ).all()
    recorded = {r.local_date for r in rows}  # any daily total = the watch synced that day
    if metric == "workouts":
        counts = dict(
            conn.execute(
                select(Observation.local_date, func.count())
                .where(
                    Observation.metric == "activity",
                    Observation.value >= WORKOUT_MIN_MINUTES,
                    Observation.local_date.between(start, end),
                )
                .group_by(Observation.local_date)
            ).all()
        )
        totals: dict[date, int | None] = {
            d: counts.get(d, 0) if d in recorded else None for d in days
        }
        inputs = {"workouts": {d.isoformat(): v for d, v in totals.items()}}
    else:
        mine = {r.local_date: r for r in rows if r.metric == metric}
        totals = {d: mine[d].value if d in mine else None for d in days}
        inputs = {
            "totals": [
                {"local_date": d.isoformat(), "observation_id": r.id, "value": r.value}
                for d, r in sorted(mine.items())
            ]
        }
    return SettlementData(daily_totals=totals), inputs


def _current_line(conn: Connection, market_id: int) -> int:
    line: int | None = conn.execute(
        select(OddsVersion.line_x10).where(
            OddsVersion.market_id == market_id, OddsVersion.is_current.is_(True)
        )
    ).scalar_one()
    if line is None:
        raise ValueError(f"market {market_id} has no line")
    return line


def settle_market(engine: Engine, clock: Clock, market_id: int) -> SettleResult:
    now = clock.now()
    with immediate(engine) as conn:
        m = conn.execute(select(Market).where(Market.id == market_id)).one()
        if m.status == MarketStatus.SETTLED.value:
            return SettleResult(market_id, False, "already_settled")
        if m.status != MarketStatus.LOCKED.value:
            return SettleResult(market_id, False, f"status_{m.status}")
        complete = latest_complete_through(conn).get(
            "weight" if m.metric == "weight" else COMPLETENESS_KEY[m.metric]
        )
        blocked = readiness(
            now=now,
            settle_after=m.settle_after,
            window_end=m.window_end,
            last_ok_sync_finished_at=last_ok_sync_finished_at(conn),
            complete_through=complete,
        )
        if blocked:
            return SettleResult(market_id, False, blocked)

        line_x10 = _current_line(conn, market_id)
        data, inputs = settlement_data(conn, m.metric, m.window_start, m.window_end)
        outcome = TEMPLATES[m.template].settle(m.params, line_x10, data)
        try:  # unique market_id: the idempotency backstop (status is checked above too)
            conn.execute(
                insert(Settlement).values(
                    market_id=market_id,
                    outcome=_outcome_json(outcome, line_x10),
                    inputs=inputs,
                    engine_version=ENGINE_VERSION,
                    created_at=now,
                )
            )
        except IntegrityError:
            return SettleResult(market_id, False, "already_settled")

        bets = _settle_legs(conn, clock, market_id, outcome, now)
        set_status(conn, [market_id], MarketStatus.LOCKED, MarketStatus.SETTLED, now)
        enqueue(
            conn,
            clock,
            category=Category.MARKET_SETTLEMENTS,
            payload={"market_id": market_id, "title": m.title} | _outcome_json(outcome, line_x10),
            dedupe_key=f"market_settled:{market_id}",
        )
    log.info("market_settled", market_id=market_id, result=outcome.reason, bets=bets)
    return SettleResult(market_id, True, outcome.reason, bets)


def _outcome_json(outcome: Outcome, line_x10: int) -> dict[str, Any]:
    return {
        "winner": outcome.winner,
        "value_x10": outcome.value_x10,
        "reason": outcome.reason,
        "line_x10": line_x10,
    }


def _settle_legs(
    conn: Connection, clock: Clock, market_id: int, outcome: Outcome, now: datetime
) -> int:
    """Singles only (parlays arrive in STAGE12): the leg result is the bet result."""
    legs = conn.execute(
        select(
            BetLeg.id.label("leg_id"),
            Bet.id.label("bet_id"),
            Bet.kind,
            Bet.account_id,
            Bet.user_id,
            Bet.stake_cents,
            Bet.potential_payout_cents,
            Selection.side,
        )
        .join(Bet, Bet.id == BetLeg.bet_id)
        .join(Selection, Selection.id == BetLeg.selection_id)
        .where(BetLeg.market_id == market_id, BetLeg.status == "open")
        .order_by(BetLeg.id)
    ).all()
    for leg in legs:
        if leg.kind != "single":
            raise NotImplementedError("parlay settlement arrives in STAGE12")
        result = leg_result(leg.side, outcome)
        paid = 0
        if result == "won":
            paid = leg.potential_payout_cents
            ledger.payout(
                conn,
                clock,
                leg.account_id,
                paid,
                idempotency_key=f"bet:{leg.bet_id}:payout",
                ref_id=leg.bet_id,
            )
        elif result == "push":
            paid = leg.stake_cents
            ledger.refund(
                conn,
                clock,
                leg.account_id,
                paid,
                idempotency_key=f"bet:{leg.bet_id}:refund",
                ref_id=leg.bet_id,
            )
        conn.execute(update(BetLeg).where(BetLeg.id == leg.leg_id).values(status=result))
        conn.execute(
            update(Bet)
            .where(Bet.id == leg.bet_id)
            .values(status=result, payout_cents=paid, settled_at=now)
        )
        enqueue(
            conn,
            clock,
            category=Category.BET_RESULTS,
            payload={
                "bet_id": leg.bet_id,
                "user_id": leg.user_id,
                "market_id": market_id,
                "side": leg.side,
                "result": result,
                "stake_cents": leg.stake_cents,
                "payout_cents": paid,
            },
            dedupe_key=f"bet_result:{leg.bet_id}",
        )
    return len(legs)


def settle_due(engine: Engine, clock: Clock) -> SettlePass:
    """Every-tick pass: settle every ready locked market; alert once on stale ones."""
    now = clock.now()
    locked = Market.status == MarketStatus.LOCKED.value
    with engine.connect() as conn:
        due = conn.execute(
            select(Market.id, Market.settle_deadline)
            .where(locked, Market.settle_after <= now)
            .order_by(Market.settle_after, Market.id)
        ).all()
        if not due:  # the common case: nothing to do, one more read for the cache
            pending: datetime | None = conn.execute(
                select(func.min(Market.settle_after)).where(locked)
            ).scalar_one()
            return SettlePass(0, 0, pending)
    settled = alerts = 0
    for market_id, deadline in due:
        result = settle_market(engine, clock, market_id)
        if result.settled:
            settled += 1
        elif deadline is not None and now >= deadline and result.reason != "already_settled":
            with immediate(engine) as conn:
                alerts += enqueue(
                    conn,
                    clock,
                    category=Category.ADMIN_ALERTS,
                    payload={
                        "kind": "stale_market",
                        "market_id": market_id,
                        "waiting_for": result.reason,
                    },
                    dedupe_key=f"stale_market:{market_id}",
                )
    with engine.connect() as conn:
        pending = conn.execute(
            select(func.min(Market.settle_after)).where(locked, Market.settle_after > now)
        ).scalar_one()
    return SettlePass(settled, alerts, pending)
