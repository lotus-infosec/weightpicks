"""Probability <-> American odds conversion and vig.

Probabilities are floats (they are not money). Rounding always favours the house:
favourites round away from zero, underdogs round toward zero, both to multiples of 5.
"""

import math
from dataclasses import dataclass

# The standard -110/-110 hold is 1/22 (~4.545%, often rounded to "4.55%").
DEFAULT_HOLD = 1 / 22
Q_MIN = 0.05
Q_MAX = 0.95
MAX_ODDS = 2000
# Float tolerance so an exact boundary (e.g. raw -110.00000000000001) is not pushed a step.
_EPS = 1e-9


@dataclass(frozen=True, slots=True)
class VigPrices:
    """Vigged implied probabilities for a two-sided market.

    `q_*` are clamped to [Q_MIN, Q_MAX] and used for pricing; `raw_*` are unclamped
    and decide whether a side is offered at all (see `is_offered`).
    """

    q_over: float
    q_under: float
    raw_over: float
    raw_under: float


def _ceil5(x: float) -> int:
    return 5 * math.ceil(x / 5 - _EPS)


def _floor5(x: float) -> int:
    return 5 * math.floor(x / 5 + _EPS)


def american(q: float) -> int:
    """American odds for vigged implied probability `q`, rounded in the house's favour."""
    if not 0.0 < q < 1.0:
        raise ValueError(f"probability must be strictly between 0 and 1, got {q!r}")
    if q >= 0.5:
        return -min(MAX_ODDS, _ceil5(100 * q / (1 - q)))
    return min(MAX_ODDS, _floor5(100 * (1 - q) / q))


def implied(odds: int) -> float:
    """Implied probability of American odds."""
    if odds <= -100:
        return -odds / (-odds + 100)
    if odds >= 100:
        return 100 / (odds + 100)
    raise ValueError(f"American odds must be <= -100 or >= 100, got {odds!r}")


def apply_vig(p_over: float, hold: float) -> VigPrices:
    """Scale fair probabilities by V = 1 / (1 - hold) and clamp for pricing."""
    if not 0.0 <= p_over <= 1.0:
        raise ValueError(f"p_over must be within [0, 1], got {p_over!r}")
    if not 0.0 <= hold < 1.0:
        raise ValueError(f"hold must be within [0, 1), got {hold!r}")
    v = 1 / (1 - hold)
    raw_over = p_over * v
    raw_under = (1 - p_over) * v
    return VigPrices(
        q_over=min(Q_MAX, max(Q_MIN, raw_over)),
        q_under=min(Q_MAX, max(Q_MIN, raw_under)),
        raw_over=raw_over,
        raw_under=raw_under,
    )


def is_offered(raw_q: float) -> bool:
    """A side whose unclamped vigged probability is below Q_MIN is not offered."""
    return raw_q >= Q_MIN
