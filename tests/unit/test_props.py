from datetime import date, timedelta
from itertools import combinations
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from app.domain import parlay
from app.domain.markets import (
    TEMPLATES,
    MarketSpec,
    MetricTotalParams,
    PricingData,
    Schedule,
    SettlementData,
    Timeframe,
    WeightChangeParams,
)
from app.domain.props import (
    BeatLastWeekParams,
    FutureChangeParams,
    MilestoneParams,
    StreakParams,
)

NY = ZoneInfo("America/New_York")
C = date(2026, 10, 5)  # creation day (a Monday)
SCHED = Schedule()


def history(days: int = 40, slope: float = -0.2, start: float = 230.0) -> dict[date, int]:
    rng = np.random.default_rng(3)
    return {
        C - timedelta(days=k): round((start + slope * (days - k) + rng.normal(0, 0.4)) * 10)
        for k in range(days)
    }


def totals(days: int = 40, mean: int = 8000) -> dict[str, dict[date, int]]:
    rng = np.random.default_rng(4)
    steps = {C - timedelta(days=k): int(rng.normal(mean, 1500)) for k in range(1, days)}
    return {"steps": steps, "workouts": {d: int(rng.random() < 0.4) for d in steps}}


def data(weigh_ins: dict[date, int] | None = None) -> PricingData:
    return PricingData(
        as_of=C,
        weigh_ins=history() if weigh_ins is None else weigh_ins,
        daily_totals=totals(),
        complete_through={"weight": C, "steps": C - timedelta(days=1)},
    )


def spec(name: str, params: object, timeframe: Timeframe = Timeframe.PROP) -> MarketSpec:
    t = TEMPLATES[name]
    return t.spec(t.params_model.model_validate(params), timeframe, SCHED, NY, "lb")


def milestone(threshold: float = 220.6, days: int = 7) -> MarketSpec:
    return spec(
        "milestone_by",
        {
            "start": C + timedelta(days=1),
            "deadline": C + timedelta(days=days),
            "threshold_x10": round(threshold * 10),
        },
    )


def test_milestone_spec_price_and_reproducibility() -> None:
    s = milestone()
    assert s.sides == ("yes", "no") and s.timeframe is Timeframe.PROP
    assert s.lock_at.astimezone(NY).date() == C  # locks the creation night
    assert s.correlation_keys[0] == f"weight:{(C + timedelta(days=1)).isoformat()}"
    assert len(s.correlation_keys) == 7
    first = TEMPLATES["milestone_by"].price(s, data(), "lb")
    second = TEMPLATES["milestone_by"].price(s, data(), "lb")
    assert first is not None and first == second  # seeded from the market's dedupe key
    assert 0.0 < first.p_over < 1.0 and first.line_x10 is None
    assert first.model_inputs["model"] == "mc_milestone"


def test_harder_milestones_are_less_likely() -> None:
    easy = TEMPLATES["milestone_by"].price(milestone(220.6), data(), "lb")
    hard = TEMPLATES["milestone_by"].price(milestone(219.8), data(), "lb")
    assert easy is not None and hard is not None and easy.p_over > hard.p_over


def test_weight_props_refuse_a_provisional_fit() -> None:
    few = dict(list(history().items())[:3])
    assert TEMPLATES["milestone_by"].price(milestone(), data(few), "lb") is None


def test_certain_props_are_not_offered() -> None:
    assert TEMPLATES["milestone_by"].price(milestone(260.0), data(), "lb") is None  # already above


def test_milestone_settles_early_and_never_pushes() -> None:
    t, s = TEMPLATES["milestone_by"], milestone(223.0)
    d1, d3 = C + timedelta(days=1), C + timedelta(days=3)
    hit = SettlementData(weigh_ins={d1: 2240, d3: 2229})
    assert t.decide_early(s.params, hit, through=d1) is None
    assert t.decide_early(s.params, hit, through=d3).winner == "yes"  # type: ignore[union-attr]
    miss = SettlementData(weigh_ins={d1: 2240})  # missing days just don't count
    assert t.decide_early(s.params, miss, through=C + timedelta(days=6)) is None
    assert t.settle(s.params, None, miss).winner == "no"


def streak(
    kind: str = "weigh_in", n: int = 4, current: int = 2, last: int | None = 2250
) -> MarketSpec:
    return spec(
        "streak_reaches",
        {
            "start": C + timedelta(days=1),
            "deadline": C + timedelta(days=6),
            "kind": kind,
            "n": n,
            "current": current,
            "last_weight_x10": last,
        },
    )


def test_streaks() -> None:
    t = TEMPLATES["streak_reaches"]
    s = streak()
    d = [C + timedelta(days=k) for k in range(1, 7)]
    reached = t.decide_early(s.params, SettlementData(weigh_ins={d[0]: 1, d[1]: 1}), d[1])
    assert reached is not None and reached.winner == "yes"
    broken = t.decide_early(s.params, SettlementData(weigh_ins={d[1]: 1}), d[0])
    assert broken is not None and broken.winner == "no" and broken.reason == "broken"
    down = streak("down", n=3, current=1, last=2250)
    lower = SettlementData(weigh_ins={d[0]: 2245, d[1]: 2240})
    assert t.decide_early(down.params, lower, d[1]).winner == "yes"  # type: ignore[union-attr]
    flat = SettlementData(weigh_ins={d[0]: 2250})
    assert t.decide_early(down.params, flat, d[0]).winner == "no"  # type: ignore[union-attr]
    price = t.price(s, data(), "lb")
    assert price is None or 0 < price.p_over < 1
    with pytest.raises(ValueError, match="already that long"):
        StreakParams(kind="weigh_in", n=2, current=2, start=d[0], deadline=d[5])
    with pytest.raises(ValueError, match="needs today"):
        StreakParams(kind="down", n=3, current=0, start=d[0], deadline=d[5])


def test_beat_last_week() -> None:
    t = TEMPLATES["beat_last_week"]
    s = spec("beat_last_week", {"metric": "steps", "start": C + timedelta(days=7)})
    p = BeatLastWeekParams.model_validate(s.params)
    assert len(s.correlation_keys) == 14 and s.window_start == C
    price = t.price(s, data(), "lb")
    assert price is not None and 0.2 < price.p_over < 0.8  # similar weeks: near a coin flip
    full = {d: 8000 for d in p.last_week} | {d: 8001 for d in p.this_week}
    assert t.settle(s.params, None, SettlementData(daily_totals=full)).winner == "yes"
    tie = {d: 8000 for d in p.last_week + p.this_week}
    assert t.settle(s.params, None, SettlementData(daily_totals=tie)).reason == "tie"
    gap = full | {p.this_week[3]: None}
    assert t.settle(s.params, None, SettlementData(daily_totals=gap)).reason == "missing_day"
    with pytest.raises(ValueError, match="Monday"):
        BeatLastWeekParams(metric="steps", start=C + timedelta(days=1))


def test_future_change() -> None:
    t = TEMPLATES["future_total_change"]
    target = C + timedelta(days=30)
    s = spec(
        "future_total_change",
        {
            "created": C,
            "day": target,
            "season_start": C - timedelta(days=39),
            "start_weight_x10": 2300,
        },
        Timeframe.FUTURE,
    )
    assert s.correlation_keys == (f"weight:{target.isoformat()}",) and s.sides == ("over", "under")
    price = t.price(s, data(), "lb")
    assert price is not None and price.line_x10 is not None and price.line_x10 < 0  # losing
    assert t.settle(s.params, -100, SettlementData(weigh_ins={target: 2150})).winner == "under"
    assert t.settle(s.params, -100, SettlementData(weigh_ins={})).reason == "missing_weigh_in"
    with pytest.raises(ValueError):
        FutureChangeParams(created=C, day=C + timedelta(days=1), season_start=C, start_weight_x10=1)


def test_params_validation() -> None:
    with pytest.raises(ValueError, match="deadline"):
        MilestoneParams(start=C, deadline=C + timedelta(days=40), threshold_x10=2200)


# ---- correlation matrix across every template ---------------------------------------------


def all_specs() -> dict[str, MarketSpec]:
    d1 = C + timedelta(days=1)
    t = TEMPLATES
    return {
        "daily_weight": t["weight_change_ou"].spec(
            WeightChangeParams(d0=C, d1=d1), Timeframe.DAILY, SCHED, NY, "lb"
        ),
        "weekly_weight": t["weight_change_ou"].spec(
            WeightChangeParams(d0=C - timedelta(days=1), d1=C + timedelta(days=6)),
            Timeframe.WEEKLY,
            SCHED,
            NY,
            "lb",
        ),
        "daily_steps": t["metric_total_ou"].spec(
            MetricTotalParams(metric="steps", start=d1, end=d1), Timeframe.DAILY, SCHED, NY, "lb"
        ),
        "weekly_kcal": t["metric_total_ou"].spec(
            MetricTotalParams(metric="kcal", start=d1, end=C + timedelta(days=7)),
            Timeframe.WEEKLY,
            SCHED,
            NY,
            "lb",
        ),
        "milestone": milestone(),
        "streak": streak(),
        "beat_steps": spec("beat_last_week", {"metric": "steps", "start": C + timedelta(days=7)}),
        "future_far": spec(
            "future_total_change",
            {
                "created": C,
                "day": C + timedelta(days=30),
                "season_start": C,
                "start_weight_x10": 2300,
            },
            Timeframe.FUTURE,
        ),
    }


BLOCKED = {
    ("daily_weight", "milestone"),
    ("daily_weight", "streak"),
    ("weekly_weight", "milestone"),
    ("weekly_weight", "streak"),
    ("milestone", "streak"),
    ("daily_steps", "beat_steps"),
}


def test_correlation_matrix() -> None:
    specs = all_specs()
    for a, b in combinations(specs, 2):
        clash = parlay.correlated([specs[a].correlation_keys, specs[b].correlation_keys])
        expected = (a, b) in BLOCKED or (b, a) in BLOCKED
        assert (clash is not None) == expected, (
            a,
            b,
            set(specs[a].correlation_keys) & set(specs[b].correlation_keys),
        )
