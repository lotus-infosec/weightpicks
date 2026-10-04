"""Parlay maths (BUILD_PLAN §1.4.2, D-041). Pure: no I/O.

Decimal odds are exact `Fraction`s: 1 + 100/|A| for negative American odds, 1 + A/100
for positive. A parlay loses the moment any leg loses; pushed or voided legs drop out;
once every leg is resolved it pays floor(stake x ∏ decimal of the won legs), or refunds
the stake if every leg dropped out. Potential payouts are capped at 100x the stake.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Literal

PAYOUT_CAP_MULTIPLE = 100  # owner decision, D-041
MIN_LEGS = 2

LegStatus = Literal["open", "won", "lost", "push", "void"]
ParlayStatus = Literal["open", "won", "lost", "push"]


def decimal(american: int) -> Fraction:
    if type(american) is not int or -100 < american < 100:
        raise ValueError(f"American odds must be <= -100 or >= 100, got {american!r}")
    return 1 + (Fraction(100, -american) if american < 0 else Fraction(american, 100))


def combined(odds: Iterable[int]) -> Fraction:
    total = Fraction(1)
    for american in odds:
        total *= decimal(american)
    return total


def potential_payout_cents(stake_cents: int, odds: Sequence[int]) -> int:
    """Total return if every leg wins, floored to whole cents."""
    if type(stake_cents) is not int or stake_cents <= 0:
        raise ValueError("stake must be positive integer cents")
    return int(stake_cents * combined(odds))


def over_cap(stake_cents: int, potential_cents: int) -> bool:
    return potential_cents > PAYOUT_CAP_MULTIPLE * stake_cents


def combined_american(odds: Sequence[int]) -> int:
    """The parlay's price as American odds (for display), rounded toward the house."""
    d = combined(odds)
    if d >= 2:
        return int((d - 1) * 100)  # floor: a smaller plus price
    return -int(-(-100 // (d - 1)))  # ceil of 100/(d-1): a longer minus price


@dataclass(frozen=True, slots=True)
class Resolution:
    status: ParlayStatus
    payout_cents: int  # total returned (0 lost, stake on push, stake x ∏ on won)


def resolve(stake_cents: int, legs: Sequence[tuple[LegStatus, int]]) -> Resolution:
    """Parlay state from its legs' statuses and pinned American odds."""
    statuses = [s for s, _ in legs]
    if "lost" in statuses:
        return Resolution("lost", 0)
    if "open" in statuses:
        return Resolution("open", 0)
    won = [odds for status, odds in legs if status == "won"]
    if not won:
        return Resolution("push", stake_cents)
    return Resolution("won", int(stake_cents * combined(won)))


def correlated(keys: Sequence[Iterable[str]]) -> tuple[int, int] | None:
    """The first pair of legs whose correlation keys intersect, else None."""
    sets = [set(k) for k in keys]
    for i in range(len(sets)):
        for j in range(i + 1, len(sets)):
            if sets[i] & sets[j]:
                return i, j
    return None
