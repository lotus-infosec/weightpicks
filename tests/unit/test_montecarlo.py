import math

import numpy as np
import pytest
from scipy.stats import norm

from app.domain.lines import UNIT_PARAMS, WeightFit
from app.domain.montecarlo import (
    N_PATHS,
    milestone_by,
    price_yes_no,
    simulate_paths,
    streak_reaches,
)


def make_fit(*, var_a: float = 0.04, cov_ab: float = -0.002, var_b: float = 0.0025) -> WeightFit:
    return WeightFit(
        a=200.0,
        b=-0.2,
        sigma=0.8,
        var_a=var_a,
        cov_ab=cov_ab,
        var_b=var_b,
        n=14,
        provisional=False,
        sigma_source="long",
        df=None,
    )


FIT = make_fit()
nan = np.nan


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


SEEDS = range(1, 5)  # 4 x 5,000 paths: sampling error ~0.35 points


def test_future_value_matches_closed_form_within_one_point() -> None:
    h = 7
    runs = [simulate_paths(FIT, h, p_weigh_in=1.0, seed=1000 + s, unit="lb") for s in SEEDS]
    mean = FIT.a + FIT.b * h
    fitted_var = FIT.var_a + 2 * h * FIT.cov_ab + h * h * FIT.var_b
    sd = math.sqrt(FIT.sigma**2 + fitted_var + h * UNIT_PARAMS["lb"].drift_q)
    for offset in (-1.0, -0.3, 0.0, 0.4, 1.2):
        line = round(mean + offset, 1) + 0.05  # half-tenth line: rounding is unbiased
        closed = float(norm.sf((line - mean) / sd))
        each = [float(np.mean(p[:, h - 1] > line)) for p in runs]  # every day weighed
        assert abs(np.mean(each) - closed) < 0.01, (offset, each, closed)
        assert all(abs(mc - closed) < 0.02 for mc in each)  # any single 5,000-path price


def test_one_day_milestone_matches_closed_form() -> None:
    fit = make_fit(var_a=0.0, cov_ab=0.0, var_b=0.0)
    threshold = 199.45
    closed = float(norm.cdf((threshold - 199.8) / 0.8))
    each = [
        float(
            np.mean(
                milestone_by(threshold, direction="down")(
                    simulate_paths(fit, 1, p_weigh_in=1.0, seed=s, unit="lb", drift_q=0.0)
                )
            )
        )
        for s in SEEDS
    ]
    assert abs(np.mean(each) - closed) < 0.01
    assert all(abs(mc - closed) < 0.02 for mc in each)


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
