"""Jinja filters: money, odds, lines and local times for display."""

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

MINUS = "\u2212"  # typographic minus for odds and lines


def money(cents: int | None) -> str:
    if cents is None:
        return "—"
    sign = MINUS if cents < 0 else ""
    whole, frac = divmod(abs(cents), 100)
    return f"{sign}${whole:,}.{frac:02d}"


def signed_money(cents: int | None) -> str:
    if cents is None:
        return "—"
    return ("+" if cents > 0 else "") + money(cents)


def odds(american: int | None) -> str:
    if american is None:
        return "off"
    return f"+{american}" if american > 0 else f"{MINUS}{abs(american)}"


def tenths(x10: int) -> str:
    """2124 -> '212.4' (a weight in tenths of the unit, as a form value)."""
    return f"{x10 // 10}.{x10 % 10}"


def line(line_x10: int | None, metric: str, unit: str) -> str:
    if line_x10 is None:
        return ""
    sign = MINUS if line_x10 < 0 else ("+" if metric == "weight" else "")
    whole, tenth = divmod(abs(line_x10), 10)
    text = f"{sign}{whole:,}.{tenth}"
    return f"{text} {unit}" if metric == "weight" else text


SIDE_LABELS = {"over": "Over", "under": "Under", "yes": "Yes", "no": "No"}


def side_hint(card: Any, side: str, unit: str) -> str:
    """What a side means in words (issue #67): over/under on a weight change is easy to
    misread, since "over -2.0" means losing less than 2.0, or gaining."""
    x10 = card.line_x10
    if x10 is None or side not in ("over", "under"):
        return ""
    if card.template == "weight_change_ou":
        amount = line(abs(x10), "weight", unit).lstrip("+")
        if x10 < 0:
            return (
                f"Lose less than {amount}, or gain"
                if side == "over"
                else f"Lose more than {amount}"
            )
        if x10 > 0:
            return (
                f"Gain more than {amount}"
                if side == "over"
                else f"Gain less than {amount}, or lose"
            )
        return "Gain weight" if side == "over" else "Lose weight"
    if card.template == "metric_total_ou":
        return f"{'More' if side == 'over' else 'Less'} than {line(x10, card.metric, unit)}"
    return ""


def market_explain(card: Any) -> str:
    """Which weigh-ins a weight change compares (issue #67)."""
    if card.template != "weight_change_ou" or card.window is None:
        return ""
    start, end = card.window
    return (
        f"Change from the {start:%a %b} {start.day} weigh-in to the {end:%a %b} {end.day} "
        "weigh-in. A minus means weight lost."
    )


def sides_data(card: Any, unit: str = "lb") -> list[dict[str, Any]]:
    """A market's sides for the bet sheet (issue #43), with what each means (#67)."""
    return [
        {
            "side": s.side,
            "label": SIDE_LABELS[s.side],
            "hint": side_hint(card, s.side, unit),
            "selection": s.selection_id,
            "odds": s.odds,
            "oddsText": odds(s.odds),
        }
        for s in card.sides
    ]


def local_time(ts: datetime, tz: ZoneInfo) -> str:
    loc = ts.astimezone(tz)
    hour = loc.hour % 12 or 12
    return f"{loc:%a %b} {loc.day}, {hour}:{loc:%M} {'AM' if loc.hour < 12 else 'PM'}"
