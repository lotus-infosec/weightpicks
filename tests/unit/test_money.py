from fractions import Fraction

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.domain.money import ZERO, Money, payout_cents

stakes = st.integers(min_value=1, max_value=10_000_000)
american_odds = st.one_of(
    st.integers(min_value=-2000, max_value=-100), st.integers(min_value=100, max_value=2000)
)


# ---- Money -------------------------------------------------------------------


@pytest.mark.parametrize("bad", [1.0, 10.5, True, "100", None])
def test_money_rejects_non_int_cents(bad: object) -> None:
    with pytest.raises(TypeError):
        Money(bad)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("cents", "text"),
    [
        (0, "$0.00"),
        (5, "$0.05"),
        (100000, "$1,000.00"),
        (123456789, "$1,234,567.89"),
        (-9090, "-$90.90"),
    ],
)
def test_money_formats_as_dollars(cents: int, text: str) -> None:
    assert str(Money(cents)) == text


@pytest.mark.parametrize(
    ("text", "cents"),
    [("1,234.56", 123456), ("$1,000", 100000), ("-12.5", -1250), ("0.05", 5), (" 7 ", 700)],
)
def test_money_parse_is_exact(text: str, cents: int) -> None:
    assert Money.parse(text) == Money(cents)


@pytest.mark.parametrize("bad", ["1.234", "abc", "", "1e3", "NaN", "$-", "1..2"])
def test_money_parse_rejects_bad_input(bad: str) -> None:
    with pytest.raises(ValueError):
        Money.parse(bad)


def test_money_arithmetic_and_ordering() -> None:
    a, b = Money(1050), Money(-50)
    assert a + b == Money(1000)
    assert a - b == Money(1100)
    assert -a == Money(-1050)
    assert a * 3 == Money(3150)
    assert b < ZERO < a
    assert sorted([a, ZERO, b]) == [b, ZERO, a]
    with pytest.raises(TypeError):
        a + 5  # type: ignore[operator]
    with pytest.raises(TypeError):
        a * 1.5  # type: ignore[operator]


@given(st.integers(min_value=-(10**12), max_value=10**12))
def test_money_str_round_trips_through_parse(cents: int) -> None:
    assert Money.parse(str(Money(cents))) == Money(cents)


# ---- Payout ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stake", "odds", "expected"),
    [
        (10000, -110, 19090),  # BUILD_PLAN §1.3 worked example: P&L +9090
        (10000, 150, 25000),
        (10000, -100, 20000),
        (10000, 100, 20000),
        (333, -110, 635),  # 302.72 profit floors to 302 (house keeps the dust)
        (1, -2000, 1),  # profit 0.05 floors to 0
    ],
)
def test_payout_golden_table(stake: int, odds: int, expected: int) -> None:
    assert payout_cents(stake, odds) == expected


@given(stakes, american_odds)
def test_payout_floors_fair_profit(stake: int, odds: int) -> None:
    fair_profit = Fraction(stake * 100, -odds) if odds < 0 else Fraction(stake * odds, 100)
    profit = payout_cents(stake, odds) - stake
    assert 0 <= profit <= fair_profit < profit + 1


@given(stakes, american_odds)
def test_payout_is_monotonic_in_stake(stake: int, odds: int) -> None:
    assert payout_cents(stake + 1, odds) >= payout_cents(stake, odds)


@pytest.mark.parametrize(("stake", "odds"), [(0, -110), (-5, 150), (100, 99), (100, -99), (100, 0)])
def test_payout_rejects_invalid_inputs(stake: int, odds: int) -> None:
    with pytest.raises(ValueError):
        payout_cents(stake, odds)


def test_payout_rejects_floats() -> None:
    with pytest.raises(TypeError):
        payout_cents(100.0, -110)  # type: ignore[arg-type]
