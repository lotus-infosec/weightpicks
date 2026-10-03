import math

import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.domain.lines import (
    UNIT_PARAMS,
    change_distribution,
    fit_weight,
    price_over_under,
    price_weight_change,
    prior_fit,
    snap_count_line,
    snap_half,
)
from app.domain.odds import DEFAULT_HOLD


def noisy_line(
    slope: float, sigma: float, days: int = 14, seed: int = 1
) -> list[tuple[int, float]]:
    rng = np.random.default_rng(seed)
    return [(t, 200 + slope * t + float(rng.normal(0, sigma))) for t in range(-(days - 1), 1)]


# ---- WLS fit ----------------------------------------------------------------------


def test_fit_recovers_slope_and_noise() -> None:
    fits = [fit_weight(noisy_line(-0.2, 0.8, seed=s), "lb") for s in range(200)]
    assert abs(np.mean([f.b for f in fits]) + 0.2) < 0.02
    assert abs(np.mean([f.sigma for f in fits]) - 0.8) < 0.1
    assert not any(f.provisional for f in fits)
    # Only 13 consecutive pairs in 14 days: short-window sigma with a Student-t.
    assert {f.sigma_source for f in fits} == {"short"}
    assert all(f.df is not None and 5 < f.df < 13 for f in fits)


def test_long_window_sigma_is_used_and_ignores_trend_changes() -> None:
    """D-029: sigma from 12 weeks of day-to-day changes, robust to a bend in the trend."""
    rng = np.random.default_rng(7)
    points = []
    for t in range(-83, 1):
        level = 200 - 0.2 * t if t < -40 else 200 - 0.2 * -40 + 0.1 * (t + 40)  # rebound
        points.append((t, level + float(rng.normal(0, 0.8))))
    fit = fit_weight(points, "lb")
    assert fit.sigma_source == "long"
    assert fit.df is None
    assert abs(fit.sigma - 0.8) < 0.12
    # The trend still comes from the last 14 days only.
    assert fit.b == pytest.approx(0.1, abs=0.08)


def test_noise_free_data_hits_the_sigma_floor() -> None:
    fit = fit_weight([(t, 200 - 0.25 * t) for t in range(-13, 1)], "lb")
    assert fit.b == pytest.approx(-0.25)
    assert fit.a == pytest.approx(200.0)
    assert fit.sigma == UNIT_PARAMS["lb"].sigma_floor == 0.3
    assert fit.se_b > 0
    assert fit_weight([(t, 90.0) for t in range(-13, 1)], "kg").sigma == 0.15


def test_only_the_last_14_days_drive_the_trend() -> None:
    recent = [(t, 200.0 + (t % 2) * 0.4) for t in range(-13, 1)]
    with_old = recent + [(t, 250.0 + (t % 3)) for t in range(-60, -14)]
    short, long = fit_weight(recent, "lb"), fit_weight(with_old, "lb")
    assert (long.a, long.b, long.n) == pytest.approx((short.a, short.b, short.n))
    # ...while older days do feed the 12-week noise estimate (D-029).
    assert (short.sigma_source, long.sigma_source) == ("short", "long")


def test_fewer_than_seven_weigh_ins_is_provisional() -> None:
    points = [(0, 200.0), (-1, 201.0), (-3, 201.5), (-6, 202.0), (-9, 203.0), (-12, 204.0)]
    fit = fit_weight(points, "lb")
    assert fit.provisional
    assert (fit.b, fit.se_b, fit.sigma, fit.sigma_source) == (0.0, 0.0, 1.0, "prior")
    weights = [2 ** (t / 7) for t, _ in points]
    expected = sum(w * y for w, (_, y) in zip(weights, points, strict=True)) / sum(weights)
    assert fit.a == pytest.approx(expected)
    assert fit_weight(points[:3], "kg").sigma == 0.45


def test_fit_needs_at_least_one_point() -> None:
    with pytest.raises(ValueError, match="weigh-in"):
        fit_weight([], "lb")


# ---- horizon distribution ------------------------------------------------------------


def test_start_known_mean_reverts_and_has_less_variance() -> None:
    fit = fit_weight(noisy_line(-0.2, 0.8), "lb")
    known_mu, known_sd = change_distribution(fit, 1, start_weight=fit.a + 1.0, unit="lb")
    unknown_mu, unknown_sd = change_distribution(fit, 1, start_weight=None, unit="lb")
    # A start weigh-in 1 lb above trend predicts a drop back toward the trend.
    assert known_mu == pytest.approx(fit.b - 1.0)
    assert unknown_mu == pytest.approx(fit.b)
    assert known_sd < unknown_sd
    q = UNIT_PARAMS["lb"].drift_q
    fitted_var = fit.var_a + 2 * fit.cov_ab + fit.var_b  # Var(a + b·1), D-029
    assert known_sd == pytest.approx(math.sqrt(fit.sigma**2 + fitted_var + q))
    assert unknown_sd == pytest.approx(math.sqrt(2 * fit.sigma**2 + fit.var_b + q))
    assert fit.var_a > 0  # level uncertainty is part of the start-known variance


def test_longer_horizons_widen() -> None:
    fit = fit_weight(noisy_line(-0.2, 0.8), "lb")
    sds = [change_distribution(fit, h, start_weight=None, unit="lb")[1] for h in (1, 7, 30)]
    assert sds == sorted(sds)


# ---- lines -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mu", "line"),
    [(-0.3, -0.5), (0.0, 0.5), (-1.0, -0.5), (-1.01, -1.5), (2.99, 2.5), (-7.4, -7.5)],
)
def test_snap_half(mu: float, line: float) -> None:
    assert snap_half(mu) == line


@pytest.mark.parametrize(("mu", "line"), [(9460, 9499.5), (9449, 9399.5), (61_240, 61_199.5)])
def test_snap_count_line(mu: float, line: float) -> None:
    assert snap_count_line(mu) == line


# ---- pricing -------------------------------------------------------------------------------


def test_student_t_widens_the_tails() -> None:
    normal = price_over_under(mu=0.0, sd=1.0, line=2.5, hold=DEFAULT_HOLD)
    heavy = price_over_under(mu=0.0, sd=1.0, line=2.5, hold=DEFAULT_HOLD, df=6)
    assert heavy.p_over > normal.p_over
    even = price_over_under(mu=0.5, sd=1.0, line=0.5, hold=DEFAULT_HOLD, df=6)
    assert even.p_over == pytest.approx(0.5)


def test_even_market_prices_minus_110_both_sides() -> None:
    """STAGE04 measure of success: p = 0.5 at the default hold -> -110/-110."""
    pricing = price_over_under(mu=-0.5, sd=1.2, line=-0.5, hold=DEFAULT_HOLD)
    assert pricing.p_over == pytest.approx(0.5)
    assert (pricing.odds_over, pricing.odds_under) == (-110, -110)
    assert pricing.line_x10 == -5


def test_extreme_side_is_not_offered() -> None:
    pricing = price_over_under(mu=0.0, sd=0.3, line=-2.5, hold=DEFAULT_HOLD)
    assert pricing.odds_over is not None
    assert pricing.odds_under is None
    assert pricing.q_under == 0.05


@given(
    st.floats(min_value=-5, max_value=5),
    st.floats(min_value=0.3, max_value=5),
    st.integers(min_value=-20, max_value=20),
)
def test_higher_line_lowers_p_over(mu: float, sd: float, k: int) -> None:
    low = price_over_under(mu=mu, sd=sd, line=k + 0.5, hold=DEFAULT_HOLD)
    high = price_over_under(mu=mu, sd=sd, line=k + 1.5, hold=DEFAULT_HOLD)
    assert high.p_over <= low.p_over


@given(st.floats(min_value=0.3, max_value=5), st.integers(min_value=-20, max_value=20))
def test_symmetric_inputs_give_symmetric_odds(sd: float, k: int) -> None:
    pricing = price_over_under(mu=k + 0.5, sd=sd, line=k + 0.5, hold=DEFAULT_HOLD)
    assert pricing.odds_over == pricing.odds_under


def test_weight_change_pricing_records_model_inputs() -> None:
    points = noisy_line(-0.2, 0.8)
    pricing = price_weight_change(points, "lb", horizon_days=1, start_weight=points[-1][1])
    assert pricing.line_x10 is not None
    assert abs(pricing.line_x10) % 10 == 5
    inputs = pricing.model_inputs
    for key in (
        "a",
        "b",
        "sigma",
        "sigma_source",
        "df",
        "se_a",
        "se_b",
        "n",
        "provisional",
        "horizon_days",
        "mu",
        "sd",
    ):
        assert key in inputs
    assert inputs["start_known"] is True
    assert pricing.odds_over is not None and pricing.odds_under is not None


def test_no_recent_weigh_ins_prices_start_unknown_from_the_prior() -> None:
    pricing = price_weight_change([], "lb", horizon_days=1, start_weight=None)
    inputs = pricing.model_inputs
    assert (inputs["n"], inputs["provisional"], inputs["sigma_source"]) == (0, True, "prior")
    assert inputs["mu"] == 0.0
    assert inputs["sd"] == pytest.approx(math.sqrt(2 * 1.0**2 + UNIT_PARAMS["lb"].drift_q))
    assert pricing.line_x10 == 5


def test_prior_fit_refuses_a_start_weight() -> None:
    with pytest.raises(ValueError, match="fitted level"):
        change_distribution(prior_fit("kg"), 1, start_weight=90.0, unit="kg")
    assert prior_fit("kg").sigma == 0.45
