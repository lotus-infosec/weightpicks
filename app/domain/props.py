"""Props and futures (BUILD_PLAN §1.4.1 step 5, §1.4.2; D-041). Pure: no I/O.

Created by the admin on local day C, a prop opens at once, locks at C's bet-lock time
and is held until it resolves; its observation window starts at C+1, so the day's
canonical weigh-in (already known) can never decide it. Weight props need a real
trend: with fewer than 7 recent weigh-ins (a provisional fit) they are not priced
(same reasoning as D-039). Milestones and streaks can settle early.
"""

import hashlib
import math
from collections.abc import Mapping
from datetime import date, timedelta
from typing import Any, ClassVar, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, model_validator
from scipy.stats import norm

from app.domain.lines import (
    MIN_WEIGH_INS,
    SIGMA_WINDOW_DAYS,
    WINDOW_DAYS,
    Pricing,
    WeightFit,
    change_distribution,
    count_model,
    fit_weight,
    price_from_probability,
    price_over_under,
    snap_half,
)
from app.domain.markets import (
    COMPLETENESS_KEY,
    METRIC_LABELS,
    MarketSpec,
    PricingData,
    Schedule,
    SettlementData,
    Timeframe,
    _build,
    _day,
    _Params,
)
from app.domain.montecarlo import milestone_by, price_yes_no, simulate_paths, streak_reaches
from app.domain.odds import DEFAULT_HOLD
from app.domain.schedule import at_local, next_local_midnight
from app.domain.settlement import Outcome
from app.domain.units import Unit

YES_NO = ("yes", "no")
WEIGH_IN_RATE_DAYS = 28
MIN_WEIGH_IN_RATE = 0.05
MAX_PROP_DAYS = 28
MAX_FUTURE_DAYS = 120


def _seed(key: str) -> int:
    return int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big") >> 1


def _days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=k) for k in range((end - start).days + 1)]


def _weight_fit(data: PricingData, unit: Unit) -> WeightFit | None:
    """The engine's fit as of the creation day, or None while it would be provisional."""
    points = [
        ((d - data.as_of).days, v / 10)
        for d, v in data.weigh_ins.items()
        if timedelta(0) <= data.as_of - d < timedelta(days=SIGMA_WINDOW_DAYS)
    ]
    recent = [t for t, _ in points if t > -WINDOW_DAYS]
    if len(recent) < MIN_WEIGH_INS:
        return None
    fit = fit_weight(points, unit)
    return None if fit.provisional else fit


def _weigh_in_rate(data: PricingData) -> float:
    days = sum(
        1 for d in data.weigh_ins if timedelta(0) <= data.as_of - d < timedelta(WEIGH_IN_RATE_DAYS)
    )
    return max(days / WEIGH_IN_RATE_DAYS, MIN_WEIGH_IN_RATE)


def _fit_inputs(fit: WeightFit, unit: Unit, hold: float) -> dict[str, Any]:
    return {
        "a": fit.a,
        "b": fit.b,
        "sigma": fit.sigma,
        "se_b": fit.se_b,
        "n": fit.n,
        "provisional": False,
        "unit": unit,
        "hold": hold,
    }


MAX_VIGGED_Q = 0.95  # the odds clamp (BUILD_PLAN §1.4.1 step 4)


def _offered(pricing: Pricing, hold: float) -> Pricing | None:
    """None when a prop is "too certain" to post: if either side's fair probability times
    the vig would exceed the odds clamp, that side's price would be better than fair, a
    free bet against the house (e.g. a milestone already all but reached)."""
    vig = 1 / (1 - hold)
    if max(pricing.p_over, 1 - pricing.p_over) * vig > MAX_VIGGED_Q:
        return None
    return pricing


class _WindowParams(_Params):
    start: date  # C + 1
    deadline: date

    @model_validator(mode="after")
    def _window(self) -> "_WindowParams":
        if not timedelta(0) <= self.deadline - self.start < timedelta(days=MAX_PROP_DAYS):
            raise ValueError(f"the deadline must be 1 to {MAX_PROP_DAYS} days out")
        return self


# ---- milestone_by ------------------------------------------------------------------------


class MilestoneParams(_WindowParams):
    threshold_x10: int = Field(gt=0)  # tenths of the unit
    direction: Literal["down", "up"] = "down"


def _reached(p: MilestoneParams, value: int) -> bool:
    return value <= p.threshold_x10 if p.direction == "down" else value >= p.threshold_x10


class MilestoneBy:
    """Yes as soon as a canonical weigh-in reaches the threshold; No after the deadline.
    Missing days just don't count; never pushes."""

    name: ClassVar[str] = "milestone_by"
    params_model: ClassVar[type[BaseModel]] = MilestoneParams
    early: ClassVar[bool] = True

    def spec(
        self, params: BaseModel, timeframe: Timeframe, schedule: Schedule, tz: ZoneInfo, unit: Unit
    ) -> MarketSpec:
        p = MilestoneParams.model_validate(params.model_dump())
        verb = "at or below" if p.direction == "down" else "at or above"
        return _build(
            self.name,
            p,
            timeframe=Timeframe.PROP,
            metric="weight",
            window=(p.start, p.deadline),
            title=f"A weigh-in {verb} {p.threshold_x10 / 10:.1f} {unit} by {_day(p.deadline)}?",
            lock_at=at_local(p.start - timedelta(days=1), schedule.bet_lock, tz),
            settle_after=at_local(p.deadline, schedule.weigh_in_end, tz),
            keys=[f"weight:{d.isoformat()}" for d in _days(p.start, p.deadline)],
            sides=YES_NO,
        )

    def price(
        self, spec: MarketSpec, data: PricingData, unit: Unit, hold: float = DEFAULT_HOLD
    ) -> Pricing | None:
        p = MilestoneParams.model_validate(spec.params)
        fit = _weight_fit(data, unit)
        if fit is None:
            return None
        horizon = (p.deadline - data.as_of).days
        rate = _weigh_in_rate(data)
        paths = simulate_paths(
            fit, horizon, p_weigh_in=rate, seed=_seed(spec.dedupe_key), unit=unit
        )
        outcomes = milestone_by(p.threshold_x10 / 10, direction=p.direction)(paths)
        inputs = _fit_inputs(fit, unit, hold) | {"model": "mc_milestone", "p_weigh_in": rate}
        return _offered(price_yes_no(outcomes, hold=hold, model_inputs=inputs), hold)

    def decide_early(
        self, params: Mapping[str, Any], data: SettlementData, through: date
    ) -> Outcome | None:
        p = MilestoneParams.model_validate(params)
        for d in _days(p.start, min(through, p.deadline)):
            value = data.weigh_ins.get(d)
            if value is not None and _reached(p, value):
                return Outcome("yes", value, "reached")
        if through >= p.deadline:
            return Outcome("no", None, "not_reached")
        return None

    def settle(
        self, params: Mapping[str, Any], line_x10: int | None, data: SettlementData
    ) -> Outcome:
        p = MilestoneParams.model_validate(params)
        decided = self.decide_early(params, data, p.deadline)
        return decided if decided is not None else Outcome("no", None, "not_reached")


# ---- streak_reaches ----------------------------------------------------------------------


class StreakParams(_WindowParams):
    kind: Literal["weigh_in", "down"]
    n: int = Field(ge=2, le=MAX_PROP_DAYS)
    current: int = Field(ge=0)  # streak length at the end of day C
    last_weight_x10: int | None = None  # day C's weigh-in, needed for a down streak

    @model_validator(mode="after")
    def _streak(self) -> "StreakParams":
        if self.kind == "down" and self.last_weight_x10 is None:
            raise ValueError("a down streak needs today's weigh-in")
        if self.n <= self.current:
            raise ValueError("the streak is already that long")
        if self.n - self.current > (self.deadline - self.start).days + 1:
            raise ValueError("the streak can't reach that length by the deadline")
        return self


class StreakReaches:
    """Yes when the streak reaches n; No as soon as it breaks or the deadline passes.
    `weigh_in`: a weigh-in every day. `down`: each weigh-in lower than the one before."""

    name: ClassVar[str] = "streak_reaches"
    params_model: ClassVar[type[BaseModel]] = StreakParams
    early: ClassVar[bool] = True

    def spec(
        self, params: BaseModel, timeframe: Timeframe, schedule: Schedule, tz: ZoneInfo, unit: Unit
    ) -> MarketSpec:
        p = StreakParams.model_validate(params.model_dump())
        what = (
            f"{p.n}-day weigh-in streak"
            if p.kind == "weigh_in"
            else (f"{p.n} lower weigh-ins in a row")
        )
        return _build(
            self.name,
            p,
            timeframe=Timeframe.PROP,
            metric="weight",
            window=(p.start, p.deadline),
            title=f"{what} by {_day(p.deadline)}? (now {p.current})",
            lock_at=at_local(p.start - timedelta(days=1), schedule.bet_lock, tz),
            settle_after=at_local(p.deadline, schedule.weigh_in_end, tz),
            keys=[f"weight:{d.isoformat()}" for d in _days(p.start, p.deadline)],
            sides=YES_NO,
        )

    def price(
        self, spec: MarketSpec, data: PricingData, unit: Unit, hold: float = DEFAULT_HOLD
    ) -> Pricing | None:
        p = StreakParams.model_validate(spec.params)
        fit = _weight_fit(data, unit)
        if fit is None:
            return None
        horizon = (p.deadline - data.as_of).days
        rate = _weigh_in_rate(data)
        paths = simulate_paths(
            fit, horizon, p_weigh_in=rate, seed=_seed(spec.dedupe_key), unit=unit
        )
        last = None if p.last_weight_x10 is None else p.last_weight_x10 / 10
        outcomes = streak_reaches(p.kind, p.n, current_streak=p.current, last_weight=last)(paths)
        inputs = _fit_inputs(fit, unit, hold) | {"model": "mc_streak", "p_weigh_in": rate}
        return _offered(price_yes_no(outcomes, hold=hold, model_inputs=inputs), hold)

    def decide_early(
        self, params: Mapping[str, Any], data: SettlementData, through: date
    ) -> Outcome | None:
        p = StreakParams.model_validate(params)
        run, previous = p.current, p.last_weight_x10
        for d in _days(p.start, min(through, p.deadline)):
            value = data.weigh_ins.get(d)
            ok = value is not None and (
                p.kind == "weigh_in" or (previous is not None and value < previous)
            )
            if not ok:
                return Outcome("no", run, "broken")
            run, previous = run + 1, value
            if run >= p.n:
                return Outcome("yes", run, "reached")
        if through >= p.deadline:
            return Outcome("no", run, "not_reached")
        return None

    def settle(
        self, params: Mapping[str, Any], line_x10: int | None, data: SettlementData
    ) -> Outcome:
        p = StreakParams.model_validate(params)
        decided = self.decide_early(params, data, p.deadline)
        return decided if decided is not None else Outcome("no", None, "not_reached")


# ---- beat_last_week -----------------------------------------------------------------------


class BeatLastWeekParams(_Params):
    metric: Literal["steps", "active_minutes", "intensity_minutes", "kcal", "workouts"]
    start: date  # Monday of the week that must beat the one before

    @model_validator(mode="after")
    def _monday(self) -> "BeatLastWeekParams":
        if self.start.weekday() != 0:
            raise ValueError("the week starts on a Monday")
        return self

    @property
    def this_week(self) -> list[date]:
        return _days(self.start, self.start + timedelta(days=6))

    @property
    def last_week(self) -> list[date]:
        return _days(self.start - timedelta(days=7), self.start - timedelta(days=1))


class BeatLastWeek:
    """This week's total beats last week's. Push on a tie or if either week has a day
    with no record at all once data is complete (as metric_total_ou)."""

    name: ClassVar[str] = "beat_last_week"
    params_model: ClassVar[type[BaseModel]] = BeatLastWeekParams
    early: ClassVar[bool] = False

    def spec(
        self, params: BaseModel, timeframe: Timeframe, schedule: Schedule, tz: ZoneInfo, unit: Unit
    ) -> MarketSpec:
        p = BeatLastWeekParams.model_validate(params.model_dump())
        end = p.start + timedelta(days=6)
        days = p.last_week + p.this_week
        return _build(
            self.name,
            p,
            timeframe=Timeframe.PROP,
            metric=p.metric,
            window=(days[0], end),
            title=f"{METRIC_LABELS[p.metric]}: week of {_day(p.start)} beats the week before?",
            lock_at=at_local(p.start - timedelta(days=1), schedule.bet_lock, tz),
            settle_after=next_local_midnight(end, tz),
            keys=[f"{p.metric}:{d.isoformat()}" for d in days],
            sides=YES_NO,
        )

    def price(
        self, spec: MarketSpec, data: PricingData, unit: Unit, hold: float = DEFAULT_HOLD
    ) -> Pricing | None:
        p = BeatLastWeekParams.model_validate(spec.params)
        yesterday = data.as_of - timedelta(days=1)
        as_of = min(yesterday, data.complete_through.get(COMPLETENESS_KEY[p.metric], yesterday))
        per_day = data.daily_totals.get(p.metric, {})
        history = sorted((d, v) for d, v in per_day.items() if d <= as_of)
        if not any(as_of - d < timedelta(days=28) for d, _ in history):
            return None
        known = sum(per_day.get(d, 0) for d in p.last_week if d <= as_of)
        rest = [d for d in p.last_week if d > as_of]
        if p.metric == "workouts":
            lam = sum(v for d, v in history if as_of - d < timedelta(days=28)) / 28
            mu_this, var_this = 7 * lam, 7 * lam
            mu_rest, var_rest = len(rest) * lam, len(rest) * lam
        else:
            mu_this, sd_this, _ = count_model(history, as_of, p.this_week)
            var_this = sd_this**2
            mu_rest, var_rest = 0.0, 0.0
            if rest:
                mu_rest, sd_rest, _ = count_model(history, as_of, rest)
                var_rest = sd_rest**2
        diff_mu = mu_this - (known + mu_rest)
        diff_sd = math.sqrt(max(var_this + var_rest, 1e-9))
        p_yes = float(norm.sf(0, loc=diff_mu, scale=diff_sd))
        inputs = {
            "model": "beat_last_week_normal",
            "metric": p.metric,
            "known_last_week": known,
            "mu_this": mu_this,
            "mu_last": known + mu_rest,
            "sd_diff": diff_sd,
            "hold": hold,
        }
        pricing = price_from_probability(p_yes, line=None, hold=hold, model_inputs=inputs)
        return _offered(pricing, hold)

    def decide_early(
        self, params: Mapping[str, Any], data: SettlementData, through: date
    ) -> Outcome | None:
        return None

    def settle(
        self, params: Mapping[str, Any], line_x10: int | None, data: SettlementData
    ) -> Outcome:
        p = BeatLastWeekParams.model_validate(params)
        this = [data.daily_totals.get(d) for d in p.this_week]
        last = [data.daily_totals.get(d) for d in p.last_week]
        if any(v is None for v in this + last):
            return Outcome(None, None, "missing_day")
        diff = sum(v for v in this if v is not None) - sum(v for v in last if v is not None)
        if diff == 0:
            return Outcome(None, 0, "tie")
        return Outcome("yes" if diff > 0 else "no", 10 * diff, "beat" if diff > 0 else "short")


# ---- future_total_change ------------------------------------------------------------------


class FutureChangeParams(_Params):
    created: date  # day C: the market locks that night
    day: date
    season_start: date
    start_weight_x10: int = Field(gt=0)  # the season's first canonical weigh-in

    @model_validator(mode="after")
    def _horizon(self) -> "FutureChangeParams":
        if not 2 <= (self.day - self.created).days <= MAX_FUTURE_DAYS:
            raise ValueError(f"the date must be 2 to {MAX_FUTURE_DAYS} days out")
        return self


class FutureTotalChange:
    """Over/under on w(day) - the season-start weight; push if that day's weigh-in is
    missing."""

    name: ClassVar[str] = "future_total_change"
    params_model: ClassVar[type[BaseModel]] = FutureChangeParams
    early: ClassVar[bool] = False

    def spec(
        self, params: BaseModel, timeframe: Timeframe, schedule: Schedule, tz: ZoneInfo, unit: Unit
    ) -> MarketSpec:
        p = FutureChangeParams.model_validate(params.model_dump())
        return _build(
            self.name,
            p,
            timeframe=Timeframe.FUTURE,
            metric="weight",
            window=(p.day, p.day),
            title=f"Weight change from season start to {_day(p.day)} ({unit})",
            lock_at=at_local(p.created, schedule.bet_lock, tz),
            settle_after=at_local(p.day, schedule.weigh_in_end, tz),
            keys=[f"weight:{p.day.isoformat()}"],
        )

    def price(
        self, spec: MarketSpec, data: PricingData, unit: Unit, hold: float = DEFAULT_HOLD
    ) -> Pricing | None:
        p = FutureChangeParams.model_validate(spec.params)
        horizon = (p.day - data.as_of).days
        if not 2 <= horizon <= MAX_FUTURE_DAYS:
            return None
        fit = _weight_fit(data, unit)
        if fit is None:
            return None
        mu, sd = change_distribution(fit, horizon, start_weight=p.start_weight_x10 / 10, unit=unit)
        line = snap_half(mu)
        inputs = _fit_inputs(fit, unit, hold) | {
            "model": "future_change",
            "horizon_days": horizon,
            "start_weight": p.start_weight_x10 / 10,
            "mu": mu,
            "sd": sd,
        }
        pricing = price_over_under(mu=mu, sd=sd, line=line, hold=hold, model_inputs=inputs)
        return _offered(pricing, hold)

    def decide_early(
        self, params: Mapping[str, Any], data: SettlementData, through: date
    ) -> Outcome | None:
        return None

    def settle(
        self, params: Mapping[str, Any], line_x10: int | None, data: SettlementData
    ) -> Outcome:
        p = FutureChangeParams.model_validate(params)
        if line_x10 is None:
            raise ValueError("a future needs a line")
        value = data.weigh_ins.get(p.day)
        if value is None:
            return Outcome(None, None, "missing_weigh_in")
        change = value - p.start_weight_x10
        if change == line_x10:
            return Outcome(None, change, "tie")
        return Outcome(
            "over" if change > line_x10 else "under",
            change,
            "over" if change > line_x10 else "under",
        )


MILESTONE_BY = MilestoneBy()
STREAK_REACHES = StreakReaches()
BEAT_LAST_WEEK = BeatLastWeek()
FUTURE_TOTAL_CHANGE = FutureTotalChange()
PROP_TEMPLATES = (MILESTONE_BY, STREAK_REACHES, BEAT_LAST_WEEK, FUTURE_TOTAL_CHANGE)
EARLY_TEMPLATES = frozenset(t.name for t in PROP_TEMPLATES if t.early)
