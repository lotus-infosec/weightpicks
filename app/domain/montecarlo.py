"""Seeded Monte Carlo for props, milestones and futures (BUILD_PLAN §1.4.1 step 5).

Paths follow the same trend + noise model as the line engine: each path draws its
own level and slope jointly from the fit's sampling distribution, the level drifts
by N(0, q) per day, and on each day a weigh-in happens with the observed rate and
reads level + N(0, sigma), rounded to a tenth like settlement. Missed days are NaN.
Templates are vectorised functions from the path matrix to per-path outcomes:
1.0 yes, 0.0 no, NaN push.
"""

import hashlib
import math
from collections.abc import Callable
from typing import Any, Literal

import numpy as np
from numpy.typing import NDArray

from app.domain.lines import UNIT_PARAMS, Pricing, WeightFit, price_from_probability
from app.domain.odds import DEFAULT_HOLD
from app.domain.units import Unit

N_PATHS = 5_000
Paths = NDArray[np.float64]  # shape (paths, days); column j is day j + 1
Outcomes = NDArray[np.float64]
Template = Callable[[Paths], Outcomes]


def mc_seed(market_id: int, odds_version: int) -> int:
    """Stable seed from (market, version) so prices reproduce in tests and audits."""
    digest = hashlib.sha256(f"{market_id}:{odds_version}".encode()).digest()
    return int.from_bytes(digest[:8], "big") >> 1


def simulate_paths(
    fit: WeightFit,
    horizon_days: int,
    *,
    p_weigh_in: float,
    seed: int,
    unit: Unit,
    n_paths: int = N_PATHS,
    drift_q: float | None = None,
) -> Paths:
    if horizon_days < 1:
        raise ValueError("horizon must be at least one day")
    q = UNIT_PARAMS[unit].drift_q if drift_q is None else drift_q
    rng = np.random.default_rng(seed)
    cov = np.array([[fit.var_a, fit.cov_ab], [fit.cov_ab, fit.var_b]])
    trend = rng.multivariate_normal([fit.a, fit.b], cov, size=n_paths, method="eigh")
    drift = np.cumsum(rng.normal(0.0, math.sqrt(q), size=(n_paths, horizon_days)), axis=1)
    days = np.arange(1, horizon_days + 1, dtype=float)
    level = trend[:, :1] + trend[:, 1:] * days + drift
    readings = np.round(level + rng.normal(0.0, fit.sigma, size=level.shape), 1)
    weighed = rng.random(level.shape) < p_weigh_in
    return np.where(weighed, readings, np.nan)


def _as_outcome(mask: NDArray[np.bool_]) -> Outcomes:
    return mask.astype(np.float64)


def milestone_by(
    threshold: float, *, direction: Literal["down", "up"], by_day: int | None = None
) -> Template:
    """Yes if any weigh-in up to `by_day` reaches the threshold; missed days don't count."""

    def template(paths: Paths) -> Outcomes:
        window = paths[:, :by_day] if by_day is not None else paths
        with np.errstate(invalid="ignore"):
            hit = window <= threshold if direction == "down" else window >= threshold
        return _as_outcome(np.any(hit, axis=1))

    return template


def streak_reaches(
    kind: str,
    n: int,
    *,
    current_streak: int = 0,
    last_weight: float | None = None,
    by_day: int | None = None,
) -> Template:
    """Yes if the streak reaches `n` before it breaks or the deadline passes.

    `weigh_in`: consecutive days with a weigh-in. `down`: consecutive weigh-ins each
    lower than the previous one (`last_weight` is the one before day 1); a missed day
    breaks a down streak.
    """
    if kind not in {"weigh_in", "down"}:
        raise ValueError(f"unknown streak kind {kind!r}")
    if kind == "down" and last_weight is None:
        raise ValueError("a down streak needs last_weight")

    def template(paths: Paths) -> Outcomes:
        window = paths[:, :by_day] if by_day is not None else paths
        if kind == "weigh_in":
            ok = ~np.isnan(window)
        else:
            previous = np.concatenate(
                [np.full((window.shape[0], 1), last_weight), window[:, :-1]], axis=1
            )
            with np.errstate(invalid="ignore"):
                ok = window < previous  # NaN on either side -> False (breaks)
        run = np.sum(np.cumprod(ok, axis=1), axis=1)
        return _as_outcome(current_streak + run >= n)

    return template


def future_value_over(*, day: int, line: float) -> Template:
    """The weigh-in on `day` is over `line`; a missed weigh-in pushes (NaN)."""

    def template(paths: Paths) -> Outcomes:
        values = paths[:, day - 1]
        with np.errstate(invalid="ignore"):
            over = (values > line).astype(np.float64)
        return np.where(np.isnan(values), np.nan, over)

    return template


def price_yes_no(
    outcomes: Outcomes,
    *,
    hold: float = DEFAULT_HOLD,
    model_inputs: dict[str, Any] | None = None,
) -> Pricing:
    decided = outcomes[~np.isnan(outcomes)]
    if decided.size == 0:
        raise ValueError("every path was a push; the market cannot be priced")
    p_yes = float(decided.mean())
    inputs = dict(model_inputs or {})
    inputs |= {
        "model": inputs.get("model", "monte_carlo"),
        "paths": int(outcomes.size),
        "pushes": int(outcomes.size - decided.size),
        "p_yes": p_yes,
    }
    return price_from_probability(p_yes, line=None, hold=hold, model_inputs=inputs)
