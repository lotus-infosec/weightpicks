"""Weight units. Raw weights are integer grams (as Garmin reports); settlement uses
tenths of the instance's display unit, converted once at ingest, rounded half-up."""

import math
import re
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction
from typing import Literal

Unit = Literal["lb", "kg"]
GRAMS_PER_POUND = Fraction(45359237, 100000)  # exact by definition
_WEIGHT = re.compile(r"\d{1,4}(?:\.\d+)?")


def parse_tenths(text: str) -> int | None:
    """'212.4' -> 2124 (tenths of the unit, rounded half-up once, like ingest). Plain
    decimals only (no exponents, signs or NaN); callers check the range they need."""
    cleaned = text.replace(",", "").strip()
    if not _WEIGHT.fullmatch(cleaned):
        return None
    return int((Decimal(cleaned) * 10).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def grams_to_tenths(grams: int, unit: Unit) -> int:
    if type(grams) is not int:
        raise TypeError("grams must be an int")
    if grams < 0:
        raise ValueError(f"weight cannot be negative, got {grams}")
    if unit == "lb":
        tenths = Fraction(grams * 10) / GRAMS_PER_POUND
    elif unit == "kg":
        tenths = Fraction(grams, 100)
    else:
        raise ValueError(f"unknown unit {unit!r}")
    return math.floor(tenths + Fraction(1, 2))
