"""Economy settings (CONCEPT §6.1). Defaults apply until /setup or the admin changes them.

`hold` is the house margin as an exact Fraction (D-024: 1/22 prices an even market at
-110/-110). It is offered as a few presets, named by the price of an even market,
because a typed percentage like 4.55% rounds to -115 (D-024).
"""

from dataclasses import asdict, dataclass, replace
from fractions import Fraction
from typing import Any

VIG_PRESETS: dict[str, Fraction] = {
    "-105": Fraction(1, 42),
    "-110": Fraction(1, 22),
    "-115": Fraction(3, 46),
    "-120": Fraction(1, 12),
}


@dataclass(frozen=True, slots=True)
class Economy:
    starting_bankroll_cents: int = 100_000  # $1,000
    daily_allowance_cents: int = 5_000  # $50
    bailout_cents: int = 50_000  # $500
    bailout_cooldown_days: int = 2
    hold: Fraction = Fraction(1, 22)  # standard -110/-110
    max_bet_cents: int | None = None  # none: all-in allowed
    max_parlay_legs: int = 6
    high_roller_cents: int = 50_000  # $500

    def __post_init__(self) -> None:
        for name in ("starting_bankroll_cents", "daily_allowance_cents", "bailout_cents"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a whole number of cents >= 0")
        if self.starting_bankroll_cents < 100:
            raise ValueError("the starting bankroll must be at least $1")
        if not 0 <= self.bailout_cooldown_days <= 30:
            raise ValueError("bailout cooldown must be 0-30 days")
        if self.hold not in VIG_PRESETS.values():
            raise ValueError("hold must be one of the vig presets")
        if self.max_bet_cents is not None and self.max_bet_cents < 100:
            raise ValueError("max bet must be at least $1 (or none)")
        if not 2 <= self.max_parlay_legs <= 12:
            raise ValueError("parlays allow 2-12 legs")
        if self.high_roller_cents < 100:
            raise ValueError("the high-roller threshold must be at least $1")

    @property
    def vig_preset(self) -> str:
        return next(name for name, value in VIG_PRESETS.items() if value == self.hold)

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["hold"] = f"{self.hold.numerator}/{self.hold.denominator}"
        return data

    @classmethod
    def from_json(cls, data: dict[str, Any] | None) -> "Economy":
        """Missing keys fall back to the defaults (older rows store `{}`)."""
        values = dict(data or {})
        if "hold" in values:
            values["hold"] = Fraction(values["hold"])
        known = {k: v for k, v in values.items() if k in cls.__dataclass_fields__}
        return replace(cls(), **known)


DEFAULT_ECONOMY = Economy()
