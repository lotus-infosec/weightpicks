from datetime import date, timedelta

import numpy as np
import pytest
from scipy.stats import poisson

from app.domain.lines import (
    COUNT_LINE_STEP,
    count_model,
    price_count_total,
    price_workouts,
    snap_count_line,
)

AS_OF = date(2026, 11, 1)  # a Sunday


def history(
    days: int, weekday: float, weekend: float, sd: float = 0.0, seed: int = 3
) -> list[tuple[date, int]]:
    rng = np.random.default_rng(seed)
    out = []
    for n in range(days):
        day = AS_OF - timedelta(days=n)
        base = weekend if day.weekday() >= 5 else weekday
        out.append((day, round(base + rng.normal(0, sd))))
    return out


def test_step_sizes_per_metric() -> None:
    assert COUNT_LINE_STEP == {
        "steps": 100,
        "kcal": 10,
        "active_minutes": 5,
        "intensity_minutes": 5,
    }
    assert snap_count_line(47.2, step=5) == 44.5
    assert snap_count_line(48.0, step=5) == 49.5
    assert snap_count_line(423, step=10) == 419.5


def test_before_four_weeks_one_mean_for_all_days() -> None:
    data = history(14, weekday=9000, weekend=6000)
    mu, sd, inputs = count_model(data, AS_OF, [AS_OF + timedelta(days=1)])
    assert not inputs["weekday_split"]
    assert 6000 < mu < 9000
    assert sd >= 0.1 * mu  # floor: 10% of the mean


def test_after_four_weeks_weekday_and_weekend_differ() -> None:
    data = history(35, weekday=9000, weekend=6000)
    monday = AS_OF + timedelta(days=1)
    saturday = AS_OF + timedelta(days=6)
    mu_mon, _, inputs = count_model(data, AS_OF, [monday])
    mu_sat, _, _ = count_model(data, AS_OF, [saturday])
    assert inputs["weekday_split"]
    assert mu_mon == pytest.approx(9000)
    assert mu_sat == pytest.approx(6000)


def test_weekly_total_sums_days_and_scales_sd() -> None:
    data = history(35, weekday=9000, weekend=6000, sd=1500)
    week = [AS_OF + timedelta(days=n) for n in range(1, 8)]
    mu, sd, _ = count_model(data, AS_OF, week)
    _, sd_day, _ = count_model(data, AS_OF, week[:1])
    assert mu == pytest.approx(5 * 9000 + 2 * 6000, rel=0.05)
    assert sd == pytest.approx(sd_day * np.sqrt(7))


def test_only_last_28_days_used() -> None:
    recent = history(28, weekday=8000, weekend=8000)
    old = [(AS_OF - timedelta(days=n), 1) for n in range(28, 60)]
    assert count_model(recent + old, AS_OF, [AS_OF + timedelta(days=1)])[0] == pytest.approx(8000)


def test_count_model_needs_history() -> None:
    with pytest.raises(ValueError, match="history"):
        count_model([], AS_OF, [AS_OF + timedelta(days=1)])


def test_price_steps_total() -> None:
    data = history(35, weekday=9000, weekend=6000, sd=1500)
    pricing = price_count_total("steps", data, AS_OF, [AS_OF + timedelta(days=1)])
    assert pricing.line_x10 is not None and pricing.line_x10 % 1000 == 995  # e.g. 8,999.5
    assert pricing.model_inputs["metric"] == "steps"
    assert pricing.odds_over is not None and pricing.odds_under is not None


def test_workouts_poisson() -> None:
    days = [(AS_OF - timedelta(days=n), 1 if n % 2 == 0 else 0) for n in range(28)]  # 14 in 28 days
    pricing = price_workouts(days, AS_OF, n_days=7)
    lam = 14 / 28 * 7
    assert pricing.line_x10 == 35  # floor(3.5) + 0.5 = 3.5
    assert pricing.p_over == pytest.approx(float(poisson.sf(3, lam)))
    assert pricing.model_inputs["lambda"] == pytest.approx(lam)


def test_no_workouts_means_over_not_offered() -> None:
    pricing = price_workouts([(AS_OF, 0)], AS_OF, n_days=7)
    assert pricing.line_x10 == 5
    assert pricing.p_over == 0.0
    assert pricing.odds_over is None
