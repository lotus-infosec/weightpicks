import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.domain.odds import (
    DEFAULT_HOLD,
    MAX_ODDS,
    Q_MAX,
    Q_MIN,
    american,
    apply_vig,
    implied,
    is_offered,
)

probs = st.floats(min_value=Q_MIN, max_value=Q_MAX, allow_nan=False)
holds = st.floats(min_value=0.0, max_value=0.2, allow_nan=False)


def test_even_market_at_default_hold_prices_minus_110_both_sides() -> None:
    prices = apply_vig(0.5, DEFAULT_HOLD)
    assert american(prices.q_over) == -110
    assert american(prices.q_under) == -110


@pytest.mark.parametrize(
    ("q", "expected"),
    [
        (0.5, -100),  # exact even money
        (11 / 21, -110),  # exact -110, must not round to -115 on float noise
        (0.6, -150),  # raw -150 exactly
        (0.61, -160),  # raw -156.41 -> favourite rounds away from zero
        (0.75, -300),
        (0.4, 150),  # raw +150 exactly
        (0.39, 155),  # raw +156.41 -> underdog rounds down
        (0.25, 300),
        (Q_MAX, -1900),
        (Q_MIN, 1900),
    ],
)
def test_american_golden_table(q: float, expected: int) -> None:
    assert american(q) == expected


@pytest.mark.parametrize(("odds", "expected"), [(-110, 110 / 210), (150, 0.4), (-100, 0.5)])
def test_implied_golden_table(odds: int, expected: float) -> None:
    assert implied(odds) == pytest.approx(expected)


@pytest.mark.parametrize("q", [0.0, 1.0, -0.1, 1.5])
def test_american_rejects_impossible_probabilities(q: float) -> None:
    with pytest.raises(ValueError, match="probability"):
        american(q)


@pytest.mark.parametrize("odds", [0, 50, -99])
def test_implied_rejects_invalid_american_odds(odds: int) -> None:
    with pytest.raises(ValueError, match="American odds"):
        implied(odds)


@given(probs)
def test_house_never_underprices(q: float) -> None:
    # The rounded price never pays more than the vigged probability allows.
    assert implied(american(q)) >= q - 1e-9


@given(probs)
def test_odds_are_bounded_multiples_of_five(q: float) -> None:
    odds = american(q)
    assert odds % 5 == 0
    assert 100 <= abs(odds) <= MAX_ODDS


@given(st.floats(min_value=0.0, max_value=1.0, allow_nan=False), holds)
def test_vig_overround_and_clamp(p_over: float, hold: float) -> None:
    prices = apply_vig(p_over, hold)
    assert prices.raw_over + prices.raw_under == pytest.approx(1 / (1 - hold))
    assert Q_MIN <= prices.q_over <= Q_MAX
    assert Q_MIN <= prices.q_under <= Q_MAX
    assert prices.q_over + prices.q_under >= 1 - 1e-12


def test_near_certain_side_is_clamped_and_flagged_not_offered() -> None:
    prices = apply_vig(0.99, DEFAULT_HOLD)
    assert prices.q_over == Q_MAX
    assert prices.q_under == Q_MIN
    assert is_offered(prices.raw_over)
    assert not is_offered(prices.raw_under)


@pytest.mark.parametrize(("p_over", "hold"), [(-0.1, 0.05), (1.1, 0.05), (0.5, -0.01), (0.5, 1.0)])
def test_apply_vig_rejects_bad_inputs(p_over: float, hold: float) -> None:
    with pytest.raises(ValueError):
        apply_vig(p_over, hold)
