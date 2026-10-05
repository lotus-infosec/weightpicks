"""Price Is Right pools, classic rules (D-010). Pure: no I/O.

Eligible guesses are at or under the actual weigh-in; the winner is the closest of
those. Ties split the pot evenly, leftover cents to the earliest entry. Nobody eligible,
no weigh-in on the target day, or no entries: every buy-in is refunded.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class PoolOutcome:
    status: str  # settled | refunded
    payouts: dict[int, int] = field(default_factory=dict)  # entry id -> cents
    winners: tuple[int, ...] = ()  # in entry order
    reason: str | None = None


def resolve(
    entries: Sequence[tuple[int, int]], actual_x10: int | None, pot_cents: int
) -> PoolOutcome:
    """`entries`: (entry id, guess in tenths) in the order they were entered."""
    ids = [entry_id for entry_id, _ in entries]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate pool entry")
    if type(pot_cents) is not int or pot_cents < 0:
        raise ValueError("the pot must be whole cents >= 0")
    if not entries:
        return PoolOutcome("refunded", reason="no_entries")
    if actual_x10 is None:
        return PoolOutcome("refunded", reason="no_weigh_in")
    eligible = [
        (entry_id, actual_x10 - guess) for entry_id, guess in entries if guess <= actual_x10
    ]
    if not eligible:
        return PoolOutcome("refunded", reason="nobody_eligible")
    best = min(gap for _, gap in eligible)
    winners = tuple(entry_id for entry_id, gap in eligible if gap == best)
    share, leftover = divmod(pot_cents, len(winners))
    payouts = {entry_id: share for entry_id in winners}
    payouts[winners[0]] += leftover
    return PoolOutcome("settled", payouts, winners)
