import itertools
from fractions import Fraction

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.domain import parlay
from app.domain.money import payout_cents

ODDS = st.one_of(st.integers(-2000, -100), st.integers(100, 2000))


def test_decimal_odds_are_exact() -> None:
    assert parlay.decimal(-110) == Fraction(21, 11)
    assert parlay.decimal(150) == Fraction(5, 2)
    assert parlay.decimal(100) == parlay.decimal(-100) == 2
    with pytest.raises(ValueError):
        parlay.decimal(50)


def test_two_leg_minus_110() -> None:
    assert parlay.potential_payout_cents(1000, [-110, -110]) == 3644  # 10 x (21/11)² = 36.446
    assert parlay.combined_american([-110, -110]) == 264


@given(st.integers(100, 10_000_000), ODDS)
def test_one_leg_matches_a_single(stake: int, odds: int) -> None:
    assert parlay.potential_payout_cents(stake, [odds]) == payout_cents(stake, odds)


@given(st.integers(100, 100_000), st.lists(ODDS, min_size=2, max_size=6))
def test_floor_and_monotonic(stake: int, odds: list[int]) -> None:
    exact = stake * parlay.combined(odds)
    paid = parlay.potential_payout_cents(stake, odds)
    assert paid <= exact < paid + 1
    assert paid >= parlay.potential_payout_cents(stake, odds[:-1])


def test_cap_edge() -> None:
    stake = 1000
    assert not parlay.over_cap(stake, 100 * stake)
    assert parlay.over_cap(stake, 100 * stake + 1)
    assert not parlay.over_cap(stake, parlay.potential_payout_cents(stake, [-110] * 7))  # ~92x
    assert parlay.over_cap(stake, parlay.potential_payout_cents(stake, [-110] * 8))  # ~176x


STATUSES = ("won", "lost", "push", "void", "open")


@pytest.mark.parametrize("n", [2, 3, 4])
def test_truth_table(n: int) -> None:
    stake, odds = 1000, [-110, 150, -200, 300][:n]
    for combo in itertools.product(STATUSES, repeat=n):
        legs = list(zip(combo, odds, strict=True))
        r = parlay.resolve(stake, legs)  # type: ignore[arg-type]
        if "lost" in combo:
            assert r == parlay.Resolution("lost", 0), combo  # an early loss settles at once
        elif "open" in combo:
            assert r == parlay.Resolution("open", 0), combo
        elif all(s in ("push", "void") for s in combo):
            assert r == parlay.Resolution("push", stake), combo  # all dropped: refund
        else:
            won = [o for s, o in legs if s == "won"]
            assert r == parlay.Resolution("won", int(stake * parlay.combined(won))), combo


def test_dropped_legs_shrink_the_payout() -> None:
    full = parlay.resolve(1000, [("won", -110), ("won", 150)])
    one_pushed = parlay.resolve(1000, [("won", -110), ("push", 150)])
    assert one_pushed.payout_cents == payout_cents(1000, -110) < full.payout_cents


def test_correlation() -> None:
    a = {"weight:2026-10-05", "weight:2026-10-06"}
    b = {"weight:2026-10-06", "weight:2026-10-07"}
    c = {"steps:2026-10-06"}
    assert parlay.correlated([a, c]) is None
    assert parlay.correlated([a, c, b]) == (0, 2)
