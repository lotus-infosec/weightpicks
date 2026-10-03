import math

import numpy as np
import pytest
from scipy.stats import norm

from app.domain.lines import UNIT_PARAMS, WeightFit
from app.domain.montecarlo import (
    N_PATHS,
    future_value_over,
    mc_seed,
    milestone_by,
    price_yes_no,
    simulate_paths,
    streak_reaches,
)

FIT = WeightFit(a=200.0, b=-0.2, sigma=0.8, se_b=0.05, n=14, provisional=False)
nan = np.nan


def test_seed_is_stable_and_distinct() -> None:
    assert mc_seed(7, 1) == mc_seed(7, 1)
    assert len({mc_seed(7, 1), mc_seed(7, 2), mc_seed(8, 1)}) == 3
    assert 0 <= mc_seed(7, 1) < 2**63


def test_paths_reproducible_by_seed() -> None:
    a = simulate_paths(FIT, 30, p_weigh_in=0.85, seed=42, unit="lb")
    b = simulate_paths(FIT, 30, p_weigh_in=0.85, seed=42, unit="lb")
    c = simulate_paths(FIT, 30, p_weigh_in=0.85, seed=43, unit="lb")
    assert a.shape == (N_PATHS, 30)
    np.testing.assert_array_equal(a, b)
    assert not np.array_equal(a, c, equal_nan=True)
    assert 0.80 < np.mean(~np.isnan(a)) < 0.90
    observed = a[~np.isnan(a)]
    np.testing.assert_allclose(observed, np.round(observed, 1))  # tenths, like settlement


def test_future_value_matches_closed_form_within_one_point() -> None:
    h = 7
    paths = simulate_paths(FIT, h, p_weigh_in=1.0, seed=mc_seed(1, 1), unit="lb")
    mean = FIT.a + FIT.b * h
    sd = math.sqrt(FIT.sigma**2 + h * h * FIT.se_b**2 + h * UNIT_PARAMS["lb"].drift_q)
    for offset in (-1.0, -0.3, 0.0, 0.4, 1.2):
        line = round(mean + offset, 1) + 0.05  # half-tenth line: rounding is unbiased
        mc = float(np.nanmean(future_value_over(day=h, line=line)(paths)))
        closed = float(norm.sf((line - mean) / sd))
        assert abs(mc - closed) < 0.01, (offset, mc, closed)


def test_one_day_milestone_matches_closed_form() -> None:
    fit = WeightFit(a=200.0, b=-0.2, sigma=0.8, se_b=0.0, n=14, provisional=False)
    paths = simulate_paths(fit, 1, p_weigh_in=1.0, seed=5, unit="lb", drift_q=0.0)
    threshold = 199.45
    mc = float(np.mean(milestone_by(threshold, direction="down")(paths)))
    closed = float(norm.cdf((threshold - 199.8) / 0.8))
    assert abs(mc - closed) < 0.01


def test_milestone_template_on_hand_made_paths() -> None:
    paths = np.array([[201.0, 199.9, nan], [201.0, nan, 200.1], [nan, nan, nan]])
    np.testing.assert_array_equal(milestone_by(200.0, direction="down")(paths), [1, 0, 0])
    np.testing.assert_array_equal(milestone_by(200.0, direction="down", by_day=1)(paths), [0, 0, 0])
    np.testing.assert_array_equal(milestone_by(201.0, direction="up")(paths), [1, 1, 0])


def test_weigh_in_streak_template() -> None:
    paths = np.array([[1.0, 1.0, 1.0], [1.0, nan, 1.0], [nan, 1.0, 1.0]])
    np.testing.assert_array_equal(streak_reaches("weigh_in", 3)(paths), [1, 0, 0])
    np.testing.assert_array_equal(streak_reaches("weigh_in", 5, current_streak=3)(paths), [1, 0, 0])
    np.testing.assert_array_equal(streak_reaches("weigh_in", 4, current_streak=3)(paths), [1, 1, 0])


def test_down_streak_template() -> None:
    paths = np.array(
        [[199.8, 199.5, 199.4], [199.8, 199.9, 199.0], [199.8, nan, 199.0], [200.5, 199.0, 198.0]]
    )
    template = streak_reaches("down", 3, last_weight=200.0)
    np.testing.assert_array_equal(template(paths), [1, 0, 0, 0])


def test_future_value_pushes_when_missed() -> None:
    paths = np.array([[200.0, 199.0], [200.0, nan], [200.0, 199.6]])
    outcome = future_value_over(day=2, line=199.5)(paths)
    np.testing.assert_array_equal(outcome, [0.0, nan, 1.0])


def test_price_yes_no_excludes_pushes() -> None:
    outcomes = np.array([1.0, 0.0, nan, 1.0, 1.0])
    pricing = price_yes_no(outcomes, model_inputs={"template": "x"})
    assert pricing.p_over == pytest.approx(0.75)
    assert pricing.line_x10 is None
    assert pricing.model_inputs["pushes"] == 1
    assert pricing.model_inputs["paths"] == 5
    with pytest.raises(ValueError, match="push"):
        price_yes_no(np.array([nan, nan]))


def test_unknown_streak_kind_rejected() -> None:
    with pytest.raises(ValueError, match="streak"):
        streak_reaches("sleep", 3)
