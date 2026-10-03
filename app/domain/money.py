"""Fake money as integer cents (AGENTS.md hard rule 4).

There is deliberately no float constructor. Payout math works on `Fraction`s of
the stored integers and floors to whole cents, so the house keeps sub-cent dust.
"""

import re
from dataclasses import dataclass
from fractions import Fraction
from typing import Self

_AMOUNT = re.compile(r"^(?P<sign>-)?\$?(?P<whole>\d{1,3}(?:,\d{3})+|\d+)(?:\.(?P<frac>\d{1,2}))?$")


@dataclass(frozen=True, slots=True, order=True)
class Money:
    cents: int

    def __post_init__(self) -> None:
        if type(self.cents) is not int:
            raise TypeError(f"Money needs int cents, got {type(self.cents).__name__}")

    @classmethod
    def parse(cls, text: str) -> Self:
        """Parse '1,234.56', '$1,000', '-12.5' exactly. At most two decimal places."""
        match = _AMOUNT.match(text.strip())
        if match is None:
            raise ValueError(f"not a money amount: {text!r}")
        whole = int(match["whole"].replace(",", ""))
        cents = whole * 100 + int((match["frac"] or "").ljust(2, "0"))
        return cls(-cents if match["sign"] else cents)

    def __add__(self, other: "Money") -> "Money":
        if not isinstance(other, Money):
            return NotImplemented
        return Money(self.cents + other.cents)

    def __sub__(self, other: "Money") -> "Money":
        if not isinstance(other, Money):
            return NotImplemented
        return Money(self.cents - other.cents)

    def __neg__(self) -> "Money":
        return Money(-self.cents)

    def __mul__(self, factor: int) -> "Money":
        if type(factor) is not int:
            return NotImplemented
        return Money(self.cents * factor)

    __rmul__ = __mul__

    def __str__(self) -> str:
        whole, frac = divmod(abs(self.cents), 100)
        return f"{'-' if self.cents < 0 else ''}${whole:,}.{frac:02d}"


ZERO = Money(0)


def payout_cents(stake_cents: int, american: int) -> int:
    """Total return (stake + profit) for a winning bet; profit is floored to whole cents."""
    if type(stake_cents) is not int or type(american) is not int:
        raise TypeError("stake and odds must be ints")
    if stake_cents <= 0:
        raise ValueError(f"stake must be positive, got {stake_cents}")
    if -100 < american < 100:
        raise ValueError(f"American odds must be <= -100 or >= 100, got {american}")
    if american < 0:
        profit = Fraction(stake_cents * 100, -american)
    else:
        profit = Fraction(stake_cents * american, 100)
    return stake_cents + int(profit)  # int() floors a non-negative Fraction
