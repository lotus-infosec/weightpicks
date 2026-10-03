"""Markets: the lifecycle state machine, the drop schedule and the template registry
(BUILD_PLAN §1.4.3, §1.4.2, D-009). Pure: callers pass dates, the zone and the data.

A template declares its params schema, how it is priced (line engine only, never AI),
how it settles, the observations its result depends on (correlation keys)
and when it locks: before its last unknown observation can exist.
"""

import json
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from enum import StrEnum
from typing import Any, ClassVar, Literal, Protocol
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, model_validator

from app.domain.lines import (
    COUNT_WINDOW_DAYS,
    SIGMA_WINDOW_DAYS,
    Pricing,
    price_count_total,
    price_weight_change,
    price_workouts,
)
from app.domain.odds import DEFAULT_HOLD
from app.domain.schedule import (
    WEIGH_IN_WINDOW_END,
    at_local,
    last_day_of_month,
    next_local_midnight,
)
from app.domain.settlement import Outcome, settle_metric_total, settle_weight_change
from app.domain.units import Unit

# ---- lifecycle ---------------------------------------------------------------------


class MarketStatus(StrEnum):
    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    OPEN = "open"
    LOCKED = "locked"
    SETTLED = "settled"
    VOIDED = "voided"
    REJECTED = "rejected"


S = MarketStatus
TRANSITIONS: Mapping[MarketStatus, frozenset[MarketStatus]] = {
    S.DRAFT: frozenset({S.PENDING_APPROVAL, S.OPEN}),
    S.PENDING_APPROVAL: frozenset({S.OPEN, S.REJECTED}),
    S.OPEN: frozenset({S.LOCKED, S.VOIDED}),
    S.LOCKED: frozenset({S.SETTLED, S.VOIDED}),
    S.SETTLED: frozenset(),
    S.VOIDED: frozenset(),
    S.REJECTED: frozenset(),
}


class InvalidTransition(ValueError):
    pass


def transition(current: MarketStatus | str, target: MarketStatus | str) -> MarketStatus:
    """The only place a market status change is decided. Returns the new status."""
    cur, tgt = MarketStatus(current), MarketStatus(target)
    if tgt not in TRANSITIONS[cur]:
        raise InvalidTransition(f"market cannot go from {cur} to {tgt}")
    return tgt


class Timeframe(StrEnum):
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


# ---- schedule (D-009 defaults; edited in /setup from STAGE08) ----------------------


@dataclass(frozen=True, slots=True)
class Schedule:
    daily_drop: time = time(11, 0)
    bet_lock: time = time(22, 0)
    weekly_drop_weekday: int = 6  # Sunday; the week is Mon-Sun
    weekly_drop: time = time(18, 0)
    monthly_drop: time = time(18, 0)  # on the last day of the month
    weigh_in_end: time = WEIGH_IN_WINDOW_END

    def __post_init__(self) -> None:
        if not 0 <= self.weekly_drop_weekday <= 6:
            raise ValueError("weekly_drop_weekday must be 0 (Mon) to 6 (Sun)")
        if self.daily_drop < self.weigh_in_end:
            raise ValueError("the daily drop must be at or after the weigh-in window closes")
        for name in ("daily_drop", "weekly_drop", "monthly_drop"):
            if getattr(self, name) >= self.bet_lock:
                raise ValueError(f"{name} must be before the bet lock time")


DEFAULT_SCHEDULE = Schedule()

# ---- metrics -------------------------------------------------------------------------

CountMarketMetric = Literal["steps", "active_minutes", "intensity_minutes", "kcal", "workouts"]
COUNT_MARKET_METRICS: tuple[CountMarketMetric, ...] = (
    "steps",
    "active_minutes",
    "intensity_minutes",
    "kcal",
    "workouts",
)
# A daily workout count (lambda well under 1) is barely a market; workouts drop weekly/monthly.
NOT_DAILY: frozenset[str] = frozenset({"workouts"})
METRIC_LABELS = {
    "weight": "Weight",
    "steps": "Steps",
    "active_minutes": "Active minutes",
    "intensity_minutes": "Intensity minutes",
    "kcal": "Calories",
    "workouts": "Workouts",
}
# Key under `complete_through` for each market metric.
COMPLETENESS_KEY = {m: m for m in COUNT_MARKET_METRICS} | {"workouts": "workout"}


def _day(d: date) -> str:
    return f"{d:%a %b} {d.day}"


# ---- specs and pricing data ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MarketSpec:
    """Everything needed to insert a market row, before pricing."""

    template: str
    timeframe: Timeframe
    metric: str
    window_start: date
    window_end: date
    params: dict[str, Any]  # JSON-safe
    title: str
    lock_at: datetime
    settle_after: datetime
    settle_deadline: datetime
    correlation_keys: tuple[str, ...]
    dedupe_key: str


@dataclass(frozen=True, slots=True)
class PricingData:
    """Observations as of the drop, gathered by the service."""

    as_of: date  # local date of the drop
    weigh_ins: Mapping[date, int] = field(default_factory=dict)  # canonical, tenths
    daily_totals: Mapping[str, Mapping[date, int]] = field(default_factory=dict)
    complete_through: Mapping[str, date] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SettlementData:
    """Observations a market settles on, gathered by the service once data is complete."""

    weigh_ins: Mapping[date, int] = field(default_factory=dict)  # canonical, tenths
    daily_totals: Mapping[date, int | None] = field(default_factory=dict)  # None = no record


SETTLE_GRACE = timedelta(hours=24)  # locked past settle_after + this -> admin alert


def dedupe_key(template: str, params: Mapping[str, Any]) -> str:
    return f"{template}:{json.dumps(params, sort_keys=True, separators=(',', ':'))}"


# ---- templates -----------------------------------------------------------------------


class Template(Protocol):
    name: ClassVar[str]
    params_model: ClassVar[type[BaseModel]]

    def spec(
        self, params: BaseModel, timeframe: Timeframe, schedule: Schedule, tz: ZoneInfo, unit: Unit
    ) -> MarketSpec: ...

    def price(
        self, spec: MarketSpec, data: PricingData, unit: Unit, hold: float = DEFAULT_HOLD
    ) -> Pricing | None: ...

    def settle(self, params: Mapping[str, Any], line_x10: int, data: SettlementData) -> Outcome: ...


class _Params(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class WeightChangeParams(_Params):
    d0: date
    d1: date

    @model_validator(mode="after")
    def _ordered(self) -> "WeightChangeParams":
        if self.d1 <= self.d0:
            raise ValueError("d1 must be after d0")
        return self


class MetricTotalParams(_Params):
    metric: CountMarketMetric
    start: date
    end: date

    @model_validator(mode="after")
    def _ordered(self) -> "MetricTotalParams":
        if self.end < self.start:
            raise ValueError("end must not be before start")
        return self

    @property
    def days(self) -> list[date]:
        return [self.start + timedelta(days=k) for k in range((self.end - self.start).days + 1)]


def _build(
    template: str,
    params: _Params,
    *,
    timeframe: Timeframe,
    metric: str,
    window: tuple[date, date],
    title: str,
    lock_at: datetime,
    settle_after: datetime,
    keys: list[str],
) -> MarketSpec:
    data = params.model_dump(mode="json")
    return MarketSpec(
        template=template,
        timeframe=timeframe,
        metric=metric,
        window_start=window[0],
        window_end=window[1],
        params=data,
        title=title,
        lock_at=lock_at,
        settle_after=settle_after,
        settle_deadline=settle_after + SETTLE_GRACE,
        correlation_keys=tuple(keys),
        dedupe_key=dedupe_key(template, data),
    )


class WeightChangeOU:
    """Over/under on w(d1) - w(d0), canonical weigh-ins in tenths of the unit."""

    name: ClassVar[str] = "weight_change_ou"
    params_model: ClassVar[type[BaseModel]] = WeightChangeParams

    def spec(
        self, params: BaseModel, timeframe: Timeframe, schedule: Schedule, tz: ZoneInfo, unit: Unit
    ) -> MarketSpec:
        p = WeightChangeParams.model_validate(params.model_dump())
        return _build(
            self.name,
            p,
            timeframe=timeframe,
            metric="weight",
            window=(p.d0, p.d1),
            title=f"Weight change, {_day(p.d0)} → {_day(p.d1)} ({unit})",
            # Lock the night of the start day (D-031): daily markets lock the night before
            # d1's weigh-in; weekly/monthly ones lock on drop night and are held to settle,
            # so nobody bets late with days of extra information against fixed odds.
            lock_at=at_local(p.d0, schedule.bet_lock, tz),
            settle_after=at_local(p.d1, schedule.weigh_in_end, tz),
            keys=[f"weight:{p.d0.isoformat()}", f"weight:{p.d1.isoformat()}"],
        )

    def price(
        self, spec: MarketSpec, data: PricingData, unit: Unit, hold: float = DEFAULT_HOLD
    ) -> Pricing | None:
        p = WeightChangeParams.model_validate(spec.params)
        if p.d0 != data.as_of:
            raise ValueError("core weight markets are priced on their start day")
        points = [
            ((day - p.d0).days, tenths / 10)
            for day, tenths in data.weigh_ins.items()
            if timedelta(0) <= p.d0 - day < timedelta(days=SIGMA_WINDOW_DAYS)
        ]
        start = data.weigh_ins.get(p.d0)
        return price_weight_change(
            points,
            unit,
            horizon_days=(p.d1 - p.d0).days,
            start_weight=None if start is None else start / 10,
            hold=hold,
        )

    def settle(self, params: Mapping[str, Any], line_x10: int, data: SettlementData) -> Outcome:
        p = WeightChangeParams.model_validate(params)
        return settle_weight_change(p.d0, p.d1, line_x10, data.weigh_ins)


class MetricTotalOU:
    """Over/under on the sum of daily totals of one count metric over [start, end]."""

    name: ClassVar[str] = "metric_total_ou"
    params_model: ClassVar[type[BaseModel]] = MetricTotalParams

    def spec(
        self, params: BaseModel, timeframe: Timeframe, schedule: Schedule, tz: ZoneInfo, unit: Unit
    ) -> MarketSpec:
        p = MetricTotalParams.model_validate(params.model_dump())
        label = METRIC_LABELS[p.metric]
        if timeframe is Timeframe.DAILY:
            title = f"{label} on {_day(p.start)}"
        elif timeframe is Timeframe.MONTHLY:
            title = f"{label} in {p.start:%B %Y}"
        else:
            title = f"{label}, {_day(p.start)} to {_day(p.end)}"
        return _build(
            self.name,
            p,
            timeframe=timeframe,
            metric=p.metric,
            window=(p.start, p.end),
            title=title,
            # Counts accrue from the first day: lock the night before the window starts.
            lock_at=at_local(p.start - timedelta(days=1), schedule.bet_lock, tz),
            settle_after=next_local_midnight(p.end, tz),
            keys=[f"{p.metric}:{d.isoformat()}" for d in p.days],
        )

    def price(
        self, spec: MarketSpec, data: PricingData, unit: Unit, hold: float = DEFAULT_HOLD
    ) -> Pricing | None:
        """None when there is no history yet (the market is skipped, not guessed)."""
        p = MetricTotalParams.model_validate(spec.params)
        yesterday = data.as_of - timedelta(days=1)
        as_of = min(yesterday, data.complete_through.get(COMPLETENESS_KEY[p.metric], yesterday))
        per_day = data.daily_totals.get(p.metric, {})
        history = sorted((d, v) for d, v in per_day.items() if d <= as_of)
        if not any(as_of - d < timedelta(days=COUNT_WINDOW_DAYS) for d, _ in history):
            return None
        if p.metric == "workouts":
            return price_workouts(history, as_of, n_days=len(p.days), hold=hold)
        return price_count_total(p.metric, history, as_of, p.days, hold=hold)

    def settle(self, params: Mapping[str, Any], line_x10: int, data: SettlementData) -> Outcome:
        p = MetricTotalParams.model_validate(params)
        return settle_metric_total(p.start, p.end, line_x10, data.daily_totals)


WEIGHT_CHANGE_OU = WeightChangeOU()
METRIC_TOTAL_OU = MetricTotalOU()
TEMPLATES: Mapping[str, Template] = {t.name: t for t in (WEIGHT_CHANGE_OU, METRIC_TOTAL_OU)}

# ---- drops ---------------------------------------------------------------------------


def drop_specs(
    timeframe: Timeframe,
    day: date,
    *,
    schedule: Schedule,
    tz: ZoneInfo,
    unit: Unit,
    enabled_metrics: Collection[str],
) -> list[MarketSpec]:
    """The core markets a drop on local date `day` posts.

    daily (D):   weight D -> D+1, and each enabled count metric for D+1.
    weekly (Sun D):  weight D -> D+7, and count totals for Mon D+1 .. Sun D+7.
    monthly (last day D): weight D -> end of next month, and next month's count totals.
    """
    if timeframe is Timeframe.DAILY:
        start, end = day + timedelta(days=1), day + timedelta(days=1)
    elif timeframe is Timeframe.WEEKLY:
        start, end = day + timedelta(days=1), day + timedelta(days=7)
    else:
        start = day + timedelta(days=1)
        end = last_day_of_month(start)
    specs = [
        WEIGHT_CHANGE_OU.spec(WeightChangeParams(d0=day, d1=end), timeframe, schedule, tz, unit)
    ]
    for metric in COUNT_MARKET_METRICS:
        if metric not in enabled_metrics or (timeframe is Timeframe.DAILY and metric in NOT_DAILY):
            continue
        params = MetricTotalParams(metric=metric, start=start, end=end)
        specs.append(METRIC_TOTAL_OU.spec(params, timeframe, schedule, tz, unit))
    return specs
