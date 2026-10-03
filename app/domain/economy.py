"""Economy defaults from CONCEPT §6. Become editable settings in /setup (STAGE08)."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Economy:
    starting_bankroll_cents: int
    daily_allowance_cents: int
    bailout_cents: int
    bailout_cooldown_days: int


DEFAULT_ECONOMY = Economy(
    starting_bankroll_cents=100_000,  # $1,000
    daily_allowance_cents=5_000,  # $50
    bailout_cents=50_000,  # $500
    bailout_cooldown_days=2,
)
