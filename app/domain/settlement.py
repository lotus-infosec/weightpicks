"""Pure settlement rules (BUILD_PLAN §1.4.2; D-013 ties push; D-032 strict push).

Every result is computed on integers: weight in tenths of the unit, counts as ints,
lines as `line_x10`. A `.5` line can still tie a tenths-precision value (C1/D-013).
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal

Side = Literal["over", "under", "yes", "no"]
LegResult = Literal["won", "lost", "push"]
ENGINE_VERSION = "settle-1"


@dataclass(frozen=True, slots=True)
class Outcome:
    winner: Side | None  # None = push
    value_x10: int | None  # the settled quantity in tenths (None when it can't be known)
    reason: str  # "over" | "under" | "tie" | "missing_weigh_in" | "missing_day"


def _compare(value_x10: int, line_x10: int) -> Outcome:
    if value_x10 > line_x10:
        return Outcome("over", value_x10, "over")
    if value_x10 < line_x10:
        return Outcome("under", value_x10, "under")
    return Outcome(None, value_x10, "tie")


def settle_weight_change(
    d0: date, d1: date, line_x10: int, weigh_ins: Mapping[date, int]
) -> Outcome:
    """w(d1) - w(d0) in tenths vs the line; either canonical weigh-in missing -> push."""
    start, end = weigh_ins.get(d0), weigh_ins.get(d1)
    if start is None or end is None:
        return Outcome(None, None, "missing_weigh_in")
    return _compare(end - start, line_x10)


def settle_metric_total(
    start: date, end: date, line_x10: int, totals: Mapping[date, int | None]
) -> Outcome:
    """Sum of daily totals vs the line; any day without a record at all -> push (A7)."""
    days = [start + timedelta(days=k) for k in range((end - start).days + 1)]
    values = [totals.get(d) for d in days]
    if any(v is None for v in values):
        return Outcome(None, None, "missing_day")
    return _compare(10 * sum(v for v in values if v is not None), line_x10)


def leg_result(side: str, outcome: Outcome) -> LegResult:
    if outcome.winner is None:
        return "push"
    return "won" if side == outcome.winner else "lost"


def readiness(
    *,
    now: datetime,
    settle_after: datetime,
    window_end: date,
    last_ok_sync_finished_at: datetime | None,
    complete_through: date | None,
) -> str | None:
    """None when the market may settle, else why not. Never settle on stale data:
    (a) now >= settle_after; (b) a successful sync finished after settle_after;
    (c) the provider says the metric is complete through window_end."""
    if now < settle_after:
        return "too_early"
    if last_ok_sync_finished_at is None or last_ok_sync_finished_at <= settle_after:
        return "no_sync_since_window"
    if complete_through is None or complete_through < window_end:
        return "data_incomplete"
    return None
