"""Why a bet or leg was voided or pushed, in the words players see (issue #44).
Shared by My Bets, the feed and the Discord posts. Pure: no I/O."""

VOID_REASONS = {
    "admin": "Voided by the admin",
    "goal_reached": "Voided: the goal was reached first",
    "timezone_changed": "Voided: the time zone changed",
}
PUSH_REASONS = {
    "tie": "Push: an exact tie with the line",
    "missing_weigh_in": "Push: no weigh-in on a day it needed",
    "missing_day": "Push: no data for a day it needed",
}


def result_reason(
    status: str, void_reason: str | None, void_note: str | None, outcome_reason: str | None
) -> str | None:
    """`status` is the bet's or leg's; the rest come from its market. None unless the
    stake came back (void or push)."""
    if status == "void":
        text = VOID_REASONS.get(void_reason or "", "Voided")
        return f"{text}: {void_note}" if void_reason == "admin" and void_note else text
    if status == "push":
        return PUSH_REASONS.get(outcome_reason or "", "Push: stake returned")
    return None
