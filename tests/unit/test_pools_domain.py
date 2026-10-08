"""Price Is Right pools, classic rules: closest without going over."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.domain.pools import PoolOutcome, resolve

POT = 10_000


def test_single_closest_without_going_over_wins() -> None:
    out = resolve([(1, 2100), (2, 2150), (3, 2160)], 2155, POT)
    assert out == PoolOutcome("settled", {2: POT}, (2,))


def test_exact_guess_wins() -> None:
    assert resolve([(1, 2155), (2, 2154)], 2155, POT).payouts == {1: POT}


def test_ties_split_with_leftover_cents_to_the_earliest_entry() -> None:
    out = resolve([(7, 2150), (3, 2150), (9, 2150), (1, 2100)], 2155, 10_001)
    # Entry order is list order (7 entered first), not id order.
    assert out.payouts == {7: 3335, 3: 3333, 9: 3333}
    assert out.winners == (7, 3, 9)


def test_everyone_over_is_a_refund() -> None:
    out = resolve([(1, 2160), (2, 2200)], 2155, POT)
    assert out == PoolOutcome("refunded", {}, (), "nobody_eligible")


def test_missing_weigh_in_is_a_refund() -> None:
    assert resolve([(1, 2100)], None, POT) == PoolOutcome("refunded", {}, (), "no_weigh_in")


def test_no_entries() -> None:
    assert resolve([], 2155, 0) == PoolOutcome("refunded", {}, (), "no_entries")


def test_one_entry_under_takes_its_own_pot() -> None:
    assert resolve([(5, 1000)], 2155, 2_500).payouts == {5: 2_500}


def test_rejects_bad_input() -> None:
    with pytest.raises(ValueError):
        resolve([(1, 2100), (1, 2000)], 2155, POT)  # duplicate entry
    with pytest.raises(ValueError):
        resolve([(1, 2100)], 2155, -1)


@given(
    guesses=st.lists(st.integers(1500, 2500), min_size=1, max_size=30),
    actual=st.one_of(st.none(), st.integers(1500, 2500)),
    buy_in=st.integers(100, 50_000),
)
def test_pot_is_conserved_and_winners_are_closest_without_going_over(
    guesses: list[int], actual: int | None, buy_in: int
) -> None:
    entries = list(enumerate(guesses, start=1))
    pot = buy_in * len(entries)
    out = resolve(entries, actual, pot)
    if out.status == "refunded":
        assert out.payouts == {}
        assert actual is None or all(g > actual for g in guesses)
        return
    assert actual is not None
    assert sum(out.payouts.values()) == pot
    best = min(actual - g for g in guesses if g <= actual)
    assert set(out.winners) == {i for i, g in entries if actual - g == best}
    shares = sorted(out.payouts.values())
    assert shares[-1] - shares[0] < len(shares)  # an even split, leftover cents only
    assert out.payouts[out.winners[0]] == max(shares)
