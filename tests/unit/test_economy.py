from fractions import Fraction

import pytest

from app.domain.economy import DEFAULT_ECONOMY, VIG_PRESETS, Economy
from app.domain.odds import american, apply_vig


def test_defaults_match_the_concept() -> None:
    e = DEFAULT_ECONOMY
    assert (e.starting_bankroll_cents, e.daily_allowance_cents, e.bailout_cents) == (
        100_000,
        5_000,
        50_000,
    )
    assert (e.bailout_cooldown_days, e.max_bet_cents, e.max_parlay_legs) == (2, None, 6)
    assert (e.high_roller_cents, e.hold, e.vig_preset) == (50_000, Fraction(1, 22), "-110")


@pytest.mark.parametrize(
    ("name", "odds"), [("-105", -105), ("-110", -110), ("-115", -115), ("-120", -120)]
)
def test_vig_presets_price_an_even_market_as_named(name: str, odds: int) -> None:
    prices = apply_vig(0.5, float(VIG_PRESETS[name]))
    assert (american(prices.q_over), american(prices.q_under)) == (odds, odds)


def test_json_round_trip_and_old_rows() -> None:
    custom = Economy(daily_allowance_cents=2_500, hold=Fraction(1, 12), max_bet_cents=20_000)
    assert Economy.from_json(custom.to_json()) == custom
    assert custom.to_json()["hold"] == "1/12"
    assert Economy.from_json({}) == DEFAULT_ECONOMY
    assert Economy.from_json(None) == DEFAULT_ECONOMY
    assert Economy.from_json({"unknown": 1, "bailout_cents": 1}).bailout_cents == 1


@pytest.mark.parametrize(
    "kwargs",
    [
        {"starting_bankroll_cents": 50},
        {"daily_allowance_cents": -1},
        {"bailout_cooldown_days": 31},
        {"hold": Fraction(1, 20)},
        {"max_bet_cents": 50},
        {"max_parlay_legs": 1},
        {"high_roller_cents": 0},
    ],
)
def test_validation(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        Economy(**kwargs)  # type: ignore[arg-type]
