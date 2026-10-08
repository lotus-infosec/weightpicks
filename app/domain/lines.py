"""The line engine: trend + noise + Monte Carlo. Pure and deterministic.

Weight = trend level + scale noise. The trend is fitted by weighted least squares
over the last 14 days of canonical weigh-ins (half-life 7 days). Scale noise sigma
is a stable property of the person and scale, so it is estimated from day-to-day
changes over the last 12 weeks; early in a season it falls back to the
14-day fit with a Student-t predictive. Lines snap to x.5; fair probabilities become
vigged American odds through `app.domain.odds`. Floats are used for probabilities
and model math only; lines leave as integer tenths.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import numpy as np
from scipy.stats import norm, poisson
from scipy.stats import t as student_t

from app.domain.odds import DEFAULT_HOLD, american, apply_vig, is_offered
from app.domain.units import Unit

WINDOW_DAYS = 14
HALF_LIFE_DAYS = 7.0
MIN_WEIGH_INS = 7
SIGMA_WINDOW_DAYS = 84  # noise from day-to-day changes over 12 weeks
MIN_SIGMA_PAIRS = 14
COUNT_WINDOW_DAYS = 28
COUNT_HALF_LIFE_DAYS = 10.0
COUNT_SD_FLOOR = 0.10  # of the mean
# Line granularity per count metric: the plan's "nearest 100" suits steps;
# minutes and kcal are an order of magnitude smaller, so they get finer steps.
COUNT_LINE_STEP: dict[str, int] = {
    "steps": 100,
    "kcal": 10,
    "active_minutes": 5,
    "intensity_minutes": 5,
}


@dataclass(frozen=True, slots=True)
class UnitParams:
    sigma_floor: float  # scale noise never assumed below this
    prior_sigma: float  # provisional-mode noise
    drift_q: float  # level-drift variance per day


UNIT_PARAMS: dict[Unit, UnitParams] = {
    "lb": UnitParams(sigma_floor=0.3, prior_sigma=1.0, drift_q=0.01),
    "kg": UnitParams(sigma_floor=0.15, prior_sigma=0.45, drift_q=0.002),  # 0.01 lb² in kg²
}


@dataclass(frozen=True, slots=True)
class WeightFit:
    a: float  # trend level at day 0 (the pricing day), display unit
    b: float  # trend per day
    sigma: float  # scale noise (floored)
    var_a: float  # sampling variance of a
    cov_ab: float
    var_b: float  # sampling variance of b
    n: int  # weigh-ins in the 14-day trend window
    provisional: bool
    sigma_source: str  # "long" (12-week day-to-day changes) | "short" (14-day fit) | "prior"
    df: float | None  # Student-t degrees of freedom when sigma is short-window, else None

    @property
    def se_b(self) -> float:
        return math.sqrt(self.var_b)


@dataclass(frozen=True, slots=True)
class Pricing:
    line_x10: int | None  # None for yes/no markets
    p_over: float  # fair probability of Over / Yes
    q_over: float  # vigged, clamped
    q_under: float
    odds_over: int | None  # None = side not offered
    odds_under: int | None
    model_inputs: dict[str, Any] = field(default_factory=dict)


def long_window_sigma(points: Sequence[tuple[int, float]]) -> tuple[float, int] | None:
    """Scale noise from consecutive-day changes over the last 12 weeks.

    Var(w_t - w_(t-1)) = 2·sigma² (+ tiny drift); subtracting the mean change removes
    the trend, so plateaus and rebounds don't inflate it. None if < 14 pairs.
    """
    by_day = dict(p for p in points if -(SIGMA_WINDOW_DAYS - 1) <= p[0] <= 0)
    changes = [by_day[t] - by_day[t - 1] for t in sorted(by_day) if t - 1 in by_day]
    if len(changes) < MIN_SIGMA_PAIRS:
        return None
    d = np.array(changes)
    return math.sqrt(float(np.sum((d - d.mean()) ** 2)) / (len(d) - 1) / 2), len(d)


def fit_weight(points: Sequence[tuple[int, float]], unit: Unit) -> WeightFit:
    """Trend by WLS over the last 14 days; sigma from 12 weeks of changes when available.

    `points` are (day_offset <= 0, weight); pass up to 84 days so sigma can use them.
    """
    params = UNIT_PARAMS[unit]
    recent = sorted((t, y) for t, y in points if -(WINDOW_DAYS - 1) <= t <= 0)
    if not recent:
        raise ValueError("need at least one weigh-in in the last 14 days")
    long = long_window_sigma(points)
    t = np.array([p[0] for p in recent], dtype=float)
    y = np.array([p[1] for p in recent], dtype=float)
    w = 2.0 ** (t / HALF_LIFE_DAYS)
    w *= len(w) / w.sum()
    n = len(recent)

    if n < MIN_WEIGH_INS:
        sigma = max(long[0], params.sigma_floor) if long else params.prior_sigma
        level = float(np.sum(w * y) / np.sum(w))
        var_level = sigma**2 * float(np.sum(w**2)) / float(np.sum(w)) ** 2
        return WeightFit(
            level, 0.0, sigma, var_level, 0.0, 0.0, n, True, "long" if long else "prior", None
        )

    # Weighted least squares with homoscedastic errors: sandwich covariance.
    x = np.column_stack([np.ones(n), t])
    xtw = x.T * w
    inv = np.linalg.inv(xtw @ x)
    beta = inv @ xtw @ y
    residuals = y - x @ beta
    if long is not None:
        sigma, df, source = max(long[0], params.sigma_floor), None, "long"
    else:
        # Unbiased weighted residual variance; Satterthwaite degrees of freedom.
        m = np.eye(n) - x @ inv @ xtw
        b_mat = m.T @ np.diag(w) @ m
        tr = float(np.trace(b_mat))
        sigma = max(math.sqrt(float(np.sum(w * residuals**2)) / tr), params.sigma_floor)
        df, source = tr**2 / float(np.trace(b_mat @ b_mat)), "short"
    cov = sigma**2 * inv @ (xtw * w) @ x @ inv
    return WeightFit(
        float(beta[0]),
        float(beta[1]),
        sigma,
        float(cov[0, 0]),
        float(cov[0, 1]),
        float(cov[1, 1]),
        n,
        False,
        source,
        df,
    )


def prior_fit(unit: Unit) -> WeightFit:
    """No weigh-ins at all yet: slope 0, prior noise. Only start-unknown markets can use
    it (the level `a` is meaningless, so `change_distribution` refuses a start weight)."""
    sigma = UNIT_PARAMS[unit].prior_sigma
    return WeightFit(0.0, 0.0, sigma, 0.0, 0.0, 0.0, 0, True, "prior", None)


def change_distribution(
    fit: WeightFit, horizon_days: int, *, start_weight: float | None, unit: Unit
) -> tuple[float, float]:
    """Mean and SD of w(start + h) - w(start).

        Start known (the start weigh-in is in hand at lock): mu = a + b·h - w_s and
        v = sigma² + Var(a + b·h) + h·q, where Var(a + b·h) includes level uncertainty
    . Start unknown: mu = b·h, v = 2·sigma² + h²·Var(b) + h·q.
    """
    if horizon_days < 1:
        raise ValueError("horizon must be at least one day")
    if fit.n == 0 and start_weight is not None:
        raise ValueError("a start weight needs a fitted level; the prior fit has none")
    h, q = float(horizon_days), UNIT_PARAMS[unit].drift_q
    if start_weight is None:
        return fit.b * h, math.sqrt(2 * fit.sigma**2 + h * h * fit.var_b + h * q)
    fitted_var = fit.var_a + 2 * h * fit.cov_ab + h * h * fit.var_b
    return fit.a + fit.b * h - start_weight, math.sqrt(fit.sigma**2 + fitted_var + h * q)


def snap_half(mu: float) -> float:
    """Nearest x.5 line: floor(mu) + 0.5 (the plan's round(mu - 0.5) + 0.5)."""
    return math.floor(mu) + 0.5


def snap_count_line(mu: float, step: int = 100) -> float:
    """Counts snap to the nearest `step`, then -0.5 (e.g. steps O/U 9,499.5)."""
    return math.floor(mu / step + 0.5) * step - 0.5


def _odds(p_over: float, hold: float) -> tuple[float, float, int | None, int | None]:
    prices = apply_vig(p_over, hold)
    over = american(prices.q_over) if is_offered(prices.raw_over) else None
    under = american(prices.q_under) if is_offered(prices.raw_under) else None
    return prices.q_over, prices.q_under, over, under


def price_from_probability(
    p_over: float,
    *,
    line: float | None,
    hold: float = DEFAULT_HOLD,
    model_inputs: dict[str, Any] | None = None,
) -> Pricing:
    q_over, q_under, odds_over, odds_under = _odds(p_over, hold)
    return Pricing(
        line_x10=None if line is None else round(line * 10),
        p_over=p_over,
        q_over=q_over,
        q_under=q_under,
        odds_over=odds_over,
        odds_under=odds_under,
        model_inputs=model_inputs or {},
    )


def price_over_under(
    *,
    mu: float,
    sd: float,
    line: float,
    hold: float = DEFAULT_HOLD,
    df: float | None = None,
    model_inputs: dict[str, Any] | None = None,
) -> Pricing:
    z = (line - mu) / sd
    p_over = float(norm.sf(z) if df is None else student_t.sf(z, df))
    return price_from_probability(p_over, line=line, hold=hold, model_inputs=model_inputs)


def price_weight_change(
    points: Sequence[tuple[int, float]],
    unit: Unit,
    *,
    horizon_days: int,
    start_weight: float | None,
    hold: float = DEFAULT_HOLD,
) -> Pricing:
    recent = any(-(WINDOW_DAYS - 1) <= t <= 0 for t, _ in points)
    fit = fit_weight(points, unit) if recent else prior_fit(unit)
    mu, sd = change_distribution(fit, horizon_days, start_weight=start_weight, unit=unit)
    line = snap_half(mu)
    inputs: dict[str, Any] = {
        "model": "weight_wls",
        "a": fit.a,
        "b": fit.b,
        "sigma": fit.sigma,
        "sigma_source": fit.sigma_source,
        "df": fit.df,
        "se_a": math.sqrt(fit.var_a),
        "se_b": fit.se_b,
        "n": fit.n,
        "provisional": fit.provisional,
        "horizon_days": horizon_days,
        "start_known": start_weight is not None,
        "start_weight": start_weight,
        "q": UNIT_PARAMS[unit].drift_q,
        "mu": mu,
        "sd": sd,
        "unit": unit,
        "hold": hold,
    }
    return price_over_under(mu=mu, sd=sd, line=line, hold=hold, df=fit.df, model_inputs=inputs)


# ---- count metrics (steps, minutes, kcal) and workouts ---------------------------------


def count_model(
    history: Sequence[tuple[date, int]], as_of: date, target_days: Sequence[date]
) -> tuple[float, float, dict[str, Any]]:
    """Mean and SD of the total over `target_days`, from the last 28 days of daily totals.

    Weighted mean (half-life 10 d); weekday/weekend means once 4 weeks of data exist;
    SD is the weighted SD floored at 10% of the mean; an n-day total has SD·sqrt(n).
    """
    recent = [
        (day, value)
        for day, value in history
        if timedelta(0) <= as_of - day < timedelta(days=COUNT_WINDOW_DAYS)
    ]
    if not recent:
        raise ValueError("no daily history in the last 28 days")
    ages = np.array([(as_of - day).days for day, _ in recent], dtype=float)
    values = np.array([value for _, value in recent], dtype=float)
    weekend = np.array([day.weekday() >= 5 for day, _ in recent])
    w = 2.0 ** (-ages / COUNT_HALF_LIFE_DAYS)
    overall = float(np.sum(w * values) / np.sum(w))

    split = len(recent) >= COUNT_WINDOW_DAYS and weekend.any() and (~weekend).any()
    if split:
        means = {
            kind: float(np.sum(w[mask] * values[mask]) / np.sum(w[mask]))
            for kind, mask in ((True, weekend), (False, ~weekend))
        }
        fitted = np.where(weekend, means[True], means[False])
    else:
        means = {True: overall, False: overall}
        fitted = np.full_like(values, overall)
    sd_day = math.sqrt(float(np.sum(w * (values - fitted) ** 2) / np.sum(w)))
    sd_day = max(sd_day, COUNT_SD_FLOOR * overall)

    mu = sum(means[day.weekday() >= 5] for day in target_days)
    sd = sd_day * math.sqrt(len(target_days))
    inputs: dict[str, Any] = {
        "model": "count_weighted_mean",
        "days_of_history": len(recent),
        "weekday_split": bool(split),
        "mean_weekday": means[False],
        "mean_weekend": means[True],
        "sd_day": sd_day,
        "target_days": [d.isoformat() for d in target_days],
        "mu": mu,
        "sd": sd,
    }
    return mu, sd, inputs


def price_count_total(
    metric: str,
    history: Sequence[tuple[date, int]],
    as_of: date,
    target_days: Sequence[date],
    *,
    hold: float = DEFAULT_HOLD,
) -> Pricing:
    mu, sd, inputs = count_model(history, as_of, target_days)
    line = snap_count_line(mu, COUNT_LINE_STEP[metric])
    inputs |= {"metric": metric, "hold": hold}
    return price_over_under(mu=mu, sd=sd, line=line, hold=hold, model_inputs=inputs)


def price_workouts(
    daily_counts: Sequence[tuple[date, int]],
    as_of: date,
    *,
    n_days: int,
    hold: float = DEFAULT_HOLD,
) -> Pricing:
    """Poisson: lambda = workouts per day over the last 28 days x n_days; line floor(lambda)+0.5."""
    recent = sum(
        count
        for day, count in daily_counts
        if timedelta(0) <= as_of - day < timedelta(days=COUNT_WINDOW_DAYS)
    )
    lam = recent / COUNT_WINDOW_DAYS * n_days
    line = snap_half(lam)
    p_over = float(poisson.sf(math.floor(line), lam)) if lam > 0 else 0.0
    inputs = {"model": "workouts_poisson", "lambda": lam, "n_days": n_days, "hold": hold}
    return price_from_probability(p_over, line=line, hold=hold, model_inputs=inputs)
