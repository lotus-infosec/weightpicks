"""Issue #67: say what Over and Under mean, and which weigh-ins a weight change compares."""

from dataclasses import dataclass
from datetime import date

import pytest

from app.web.format import market_explain, side_hint, sides_data


@dataclass
class Side:
    side: str
    selection_id: int = 1
    odds: int | None = -110


@dataclass
class Card:
    template: str
    line_x10: int | None
    metric: str = "weight"
    window: tuple[date, date] | None = (date(2026, 10, 5), date(2026, 10, 6))
    sides: tuple[Side, ...] = (Side("over"), Side("under"))


@pytest.mark.parametrize(
    ("line", "over", "under"),
    [
        (-20, "Lose less than 2.0 lb, or gain", "Lose more than 2.0 lb"),
        (15, "Gain more than 1.5 lb", "Gain less than 1.5 lb, or lose"),
        (0, "Gain weight", "Lose weight"),
    ],
)
def test_weight_change_sides_in_words(line: int, over: str, under: str) -> None:
    card = Card("weight_change_ou", line)
    assert (side_hint(card, "over", "lb"), side_hint(card, "under", "lb")) == (over, under)


def test_counts_kg_yes_no_and_the_sheet_data() -> None:
    steps = Card("metric_total_ou", 85005, metric="steps")
    assert side_hint(steps, "over", "lb") == "More than 8,500.5"
    assert side_hint(steps, "under", "lb") == "Less than 8,500.5"
    assert side_hint(Card("weight_change_ou", -5), "under", "kg") == "Lose more than 0.5 kg"
    assert side_hint(Card("milestone", None), "yes", "lb") == ""
    hints = [s["hint"] for s in sides_data(Card("weight_change_ou", -20))]
    assert hints == ["Lose less than 2.0 lb, or gain", "Lose more than 2.0 lb"]


def test_explain_names_both_weigh_ins() -> None:
    assert market_explain(Card("weight_change_ou", -20)) == (
        "Change from the Mon Oct 5 weigh-in to the Tue Oct 6 weigh-in. A minus means weight lost."
    )
    assert market_explain(Card("metric_total_ou", 1, metric="steps")) == ""
