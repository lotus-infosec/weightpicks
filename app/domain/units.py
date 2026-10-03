"""Weight units. Raw weights are integer grams (as Garmin reports); settlement uses
tenths of the instance's display unit, converted once at ingest, rounded half-up."""

import math
from fractions import Fraction
from typing import Literal

Unit = Literal["lb", "kg"]
GRAMS_PER_POUND = Fraction(45359237, 100000)  # exact by definition


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
