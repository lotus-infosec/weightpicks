from datetime import UTC, date, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.domain.settlement import (
    Outcome,
    leg_result,
    readiness,
    settle_metric_total,
    settle_weight_change,
)

D0, D1 = date(2026, 10, 5), date(2026, 10, 6)

# ---- weight change truth table ------------------------------------------------------


@pytest.mark.parametrize(
    ("w0", "w1", "line_x10", "winner", "reason", "value"),
    [
        (2000, 1990, -5, "under", "under", -10),  # -1.0 vs -0.5
        (2000, 1997, -5, "over", "over", -3),  # -0.3 vs -0.5
        (2000, 1995, -5, None, "tie", -5),  # exactly the line: push (D-013)
        (2000, 2015, 15, None, "tie", 15),
        (2000, 2000, -5, "over", "over", 0),
        (2000, 2000, 5, "under", "under", 0),
        (1500, 1400, -75, "under", "under", -100),  # big weekly drop
        (1500, 1430, -75, "over", "over", -70),
    ],
)
def test_weight_change_truth_table(
    w0: int, w1: int, line_x10: int, winner: str | None, reason: str, value: int
) -> None:
    out = settle_weight_change(D0, D1, line_x10, {D0: w0, D1: w1})
    assert out == Outcome(winner, value, reason)  # type: ignore[arg-type]


@pytest.mark.parametrize("weigh_ins", [{D1: 2000}, {D0: 2000}, {}])
def test_missing_weigh_in_pushes(weigh_ins: dict[date, int]) -> None:
    assert settle_weight_change(D0, D1, -5, weigh_ins) == Outcome(None, None, "missing_weigh_in")


def test_other_days_are_ignored() -> None:
    weekly_end = D0 + timedelta(days=7)
    data = {D0: 2000, D1: 1950, weekly_end: 1985}
    assert settle_weight_change(D0, weekly_end, -25, data).winner == "over"  # -1.5 > -2.5


# ---- metric totals truth table --------------------------------------------------------


def test_daily_total_truth_table() -> None:
    assert settle_metric_total(D1, D1, 94995, {D1: 9500}).winner == "over"
    assert settle_metric_total(D1, D1, 94995, {D1: 9499}).winner == "under"
    assert settle_metric_total(D1, D1, 94995, {D1: 9500}).value_x10 == 95000
    assert settle_metric_total(D1, D1, 94995, {D1: None}).reason == "missing_day"
    assert settle_metric_total(D1, D1, 94995, {}).reason == "missing_day"


def test_weekly_total_needs_every_day() -> None:
    start, end = date(2026, 10, 5), date(2026, 10, 11)
    totals: dict[date, int | None] = {start + timedelta(days=k): 1 for k in range(7)}
    assert settle_metric_total(start, end, 35, totals).winner == "over"  # 7 > 3.5
    assert settle_metric_total(start, end, 75, totals).winner == "under"
    zero_days = dict.fromkeys(totals, 0)  # recorded zero-workout days are real
    assert settle_metric_total(start, end, 5, zero_days).winner == "under"
    totals[start + timedelta(days=3)] = None
    assert settle_metric_total(start, end, 35, totals) == Outcome(None, None, "missing_day")


def test_integer_line_ties_push() -> None:
    assert settle_metric_total(D1, D1, 95000, {D1: 9500}).reason == "tie"


@given(st.integers(-500, 500), st.integers(-500, 500))
def test_over_under_push_are_exclusive_and_exhaustive(change: int, line_x10: int) -> None:
    out = settle_weight_change(D0, D1, line_x10, {D0: 2000, D1: 2000 + change})
    results = {leg_result("over", out), leg_result("under", out)}
    if change == line_x10:
        assert results == {"push"}
    else:
        assert results == {"won", "lost"}
        assert (out.winner == "over") == (change > line_x10)


# ---- readiness gate ---------------------------------------------------------------------

AFTER = datetime(2026, 10, 6, 15, 0, tzinfo=UTC)  # d1 window close (11:00 EDT)


def gate(**overrides: object) -> str | None:
    args: dict[str, object] = {
        "now": AFTER + timedelta(minutes=5),
        "settle_after": AFTER,
        "window_end": D1,
        "last_ok_sync_finished_at": AFTER + timedelta(minutes=1),
        "complete_through": D1,
    }
    args.update(overrides)
    return readiness(**args)  # type: ignore[arg-type]


def test_gate_passes_when_all_three_hold() -> None:
    assert gate() is None


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"now": AFTER - timedelta(seconds=1)}, "too_early"),
        ({"last_ok_sync_finished_at": None}, "no_sync_since_window"),
        ({"last_ok_sync_finished_at": AFTER}, "no_sync_since_window"),
        ({"last_ok_sync_finished_at": AFTER - timedelta(hours=1)}, "no_sync_since_window"),
        ({"complete_through": None}, "data_incomplete"),
        ({"complete_through": D0}, "data_incomplete"),
    ],
)
def test_gate_blocks_on_each_condition(overrides: dict[str, object], reason: str) -> None:
    assert gate(**overrides) == reason
