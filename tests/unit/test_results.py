import pytest

from app.domain.results import result_reason


@pytest.mark.parametrize(
    ("status", "code", "note", "outcome", "expected"),
    [
        ("void", "admin", "Scale broke", None, "Voided by the admin: Scale broke"),
        ("void", "admin", None, None, "Voided by the admin"),
        ("void", "goal_reached", None, None, "Voided: the goal was reached first"),
        ("void", "timezone_changed", None, None, "Voided: the time zone changed"),
        ("void", None, None, None, "Voided"),
        ("push", None, None, "tie", "Push: an exact tie with the line"),
        ("push", None, None, "missing_weigh_in", "Push: no weigh-in on a day it needed"),
        ("push", None, None, "missing_day", "Push: no data for a day it needed"),
        ("push", None, None, None, "Push: stake returned"),
        ("won", "admin", "x", "over", None),
        ("lost", None, None, "under", None),
        ("open", None, None, None, None),
    ],
)
def test_result_reason(
    status: str, code: str | None, note: str | None, outcome: str | None, expected: str | None
) -> None:
    assert result_reason(status, code, note, outcome) == expected
