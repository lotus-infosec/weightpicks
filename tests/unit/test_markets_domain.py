from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.domain.markets import (
    DEFAULT_SCHEDULE,
    METRIC_TOTAL_OU,
    TEMPLATES,
    TRANSITIONS,
    WEIGHT_CHANGE_OU,
    InvalidTransition,
    MarketStatus,
    MetricTotalParams,
    PricingData,
    Schedule,
    Timeframe,
    WeightChangeParams,
    drop_specs,
    transition,
)
from app.domain.schedule import (
    at_local,
    last_day_of_month,
    latest_daily,
    latest_month_end,
    latest_weekly,
)

NY = ZoneInfo("America/New_York")
ALL = ("steps", "active_minutes", "intensity_minutes", "kcal", "workouts")
S = MarketStatus


def local(y: int, m: int, d: int, hh: int, mm: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=NY).astimezone(UTC)


# ---- state machine ---------------------------------------------------------------------

ALLOWED = {
    (S.DRAFT, S.PENDING_APPROVAL),
    (S.DRAFT, S.OPEN),
    (S.PENDING_APPROVAL, S.OPEN),
    (S.PENDING_APPROVAL, S.REJECTED),
    (S.OPEN, S.LOCKED),
    (S.OPEN, S.VOIDED),
    (S.LOCKED, S.SETTLED),
    (S.LOCKED, S.VOIDED),
}


@pytest.mark.parametrize("current", list(S))
@pytest.mark.parametrize("target", list(S))
def test_transition_table(current: MarketStatus, target: MarketStatus) -> None:
    if (current, target) in ALLOWED:
        assert transition(current, target) is target
    else:
        with pytest.raises(InvalidTransition):
            transition(current, target)


def test_terminal_states_have_no_exits() -> None:
    assert {s for s, exits in TRANSITIONS.items() if not exits} == {
        S.SETTLED,
        S.VOIDED,
        S.REJECTED,
    }
    assert transition("open", "locked") == "locked"  # plain strings from the DB work


# ---- schedule helpers ------------------------------------------------------------------


def test_lock_at_across_the_nov_1_2026_dst_change() -> None:
    """22:00 local is 02:00 UTC in EDT and 03:00 UTC in EST."""
    before = WEIGHT_CHANGE_OU.spec(
        WeightChangeParams(d0=date(2026, 10, 30), d1=date(2026, 10, 31)),
        Timeframe.DAILY,
        DEFAULT_SCHEDULE,
        NY,
        "lb",
    )
    over = WEIGHT_CHANGE_OU.spec(
        WeightChangeParams(d0=date(2026, 10, 31), d1=date(2026, 11, 1)),
        Timeframe.DAILY,
        DEFAULT_SCHEDULE,
        NY,
        "lb",
    )
    after = WEIGHT_CHANGE_OU.spec(
        WeightChangeParams(d0=date(2026, 11, 1), d1=date(2026, 11, 2)),
        Timeframe.DAILY,
        DEFAULT_SCHEDULE,
        NY,
        "lb",
    )
    assert before.lock_at == datetime(2026, 10, 31, 2, 0, tzinfo=UTC)  # Oct 30 22:00 EDT
    assert over.lock_at == datetime(2026, 11, 1, 2, 0, tzinfo=UTC)  # Oct 31 22:00 EDT
    assert after.lock_at == datetime(2026, 11, 2, 3, 0, tzinfo=UTC)  # Nov 1 22:00 EST
    # The weigh-in window of Nov 1 closes at 11:00 EST.
    assert over.settle_after == datetime(2026, 11, 1, 16, 0, tzinfo=UTC)
    for spec in (before, over, after):
        assert spec.lock_at.astimezone(NY).time() == time(22, 0)


def test_spring_forward_gap_resolves_after_the_gap() -> None:
    assert at_local(date(2027, 3, 14), time(2, 30), NY).astimezone(NY).time() == time(3, 30)


@pytest.mark.parametrize(
    ("day", "last"),
    [
        (date(2026, 1, 15), date(2026, 1, 31)),
        (date(2026, 2, 1), date(2026, 2, 28)),
        (date(2028, 2, 10), date(2028, 2, 29)),
        (date(2026, 4, 30), date(2026, 4, 30)),
        (date(2026, 12, 31), date(2026, 12, 31)),
    ],
)
def test_last_day_of_month(day: date, last: date) -> None:
    assert last_day_of_month(day) == last


def test_latest_daily() -> None:
    assert latest_daily(local(2026, 10, 5, 10, 59), time(11), NY) == date(2026, 10, 4)
    assert latest_daily(local(2026, 10, 5, 11, 0), time(11), NY) == date(2026, 10, 5)
    assert latest_daily(local(2026, 11, 1, 11, 0), time(11), NY) == date(2026, 11, 1)


def test_latest_weekly_is_sunday_18_00() -> None:
    sunday = date(2026, 10, 4)
    assert latest_weekly(local(2026, 10, 4, 17, 59), 6, time(18), NY) == sunday - timedelta(7)
    assert latest_weekly(local(2026, 10, 4, 18, 0), 6, time(18), NY) == sunday
    assert latest_weekly(local(2026, 10, 10, 23, 0), 6, time(18), NY) == sunday


def test_latest_month_end() -> None:
    assert latest_month_end(local(2026, 10, 31, 17, 59), time(18), NY) == date(2026, 9, 30)
    assert latest_month_end(local(2026, 10, 31, 18, 0), time(18), NY) == date(2026, 10, 31)
    assert latest_month_end(local(2026, 11, 15, 9, 0), time(18), NY) == date(2026, 10, 31)
    assert latest_month_end(local(2026, 3, 1, 0, 30), time(18), NY) == date(2026, 2, 28)


@given(st.datetimes(min_value=datetime(2026, 1, 1), max_value=datetime(2030, 12, 31)))  # noqa: DTZ001
def test_latest_occurrences_are_never_in_the_future(naive: datetime) -> None:
    now = naive.replace(tzinfo=UTC)
    sched = DEFAULT_SCHEDULE
    daily = latest_daily(now, sched.daily_drop, NY)
    weekly = latest_weekly(now, 6, sched.weekly_drop, NY)
    monthly = latest_month_end(now, sched.monthly_drop, NY)
    assert at_local(daily, sched.daily_drop, NY) <= now
    assert now - at_local(daily, sched.daily_drop, NY) < timedelta(hours=25)
    assert weekly.weekday() == 6 and at_local(weekly, sched.weekly_drop, NY) <= now
    assert now - at_local(weekly, sched.weekly_drop, NY) < timedelta(days=7, hours=1)
    assert monthly == last_day_of_month(monthly)
    assert at_local(monthly, sched.monthly_drop, NY) <= now


def test_schedule_validation() -> None:
    with pytest.raises(ValueError, match="before the bet lock"):
        Schedule(bet_lock=time(10, 0), daily_drop=time(11, 0))
    with pytest.raises(ValueError, match="weigh-in window"):
        Schedule(daily_drop=time(9, 0))
    with pytest.raises(ValueError, match="weekday"):
        Schedule(weekly_drop_weekday=7)


# ---- drops -----------------------------------------------------------------------------


def specs(timeframe: Timeframe, day: date, enabled: tuple[str, ...] = ALL) -> list:  # type: ignore[type-arg]
    return drop_specs(
        timeframe, day, schedule=DEFAULT_SCHEDULE, tz=NY, unit="lb", enabled_metrics=enabled
    )


def test_daily_drop() -> None:
    day = date(2026, 10, 5)
    out = specs(Timeframe.DAILY, day)
    assert [(s.template, s.metric) for s in out] == [
        ("weight_change_ou", "weight"),
        ("metric_total_ou", "steps"),
        ("metric_total_ou", "active_minutes"),
        ("metric_total_ou", "intensity_minutes"),
        ("metric_total_ou", "kcal"),
    ]
    weight, steps = out[0], out[1]
    assert weight.params == {"d0": "2026-10-05", "d1": "2026-10-06"}
    assert weight.correlation_keys == ("weight:2026-10-05", "weight:2026-10-06")
    assert weight.title == "Weight change, Mon Oct 5 → Tue Oct 6 (lb)"
    assert steps.params == {"metric": "steps", "start": "2026-10-06", "end": "2026-10-06"}
    assert steps.correlation_keys == ("steps:2026-10-06",)
    assert steps.title == "Steps on Tue Oct 6"
    # Both lock at 22:00 tonight; steps settle after midnight ending Oct 6.
    assert {s.lock_at for s in out} == {local(2026, 10, 5, 22)}
    assert steps.settle_after == local(2026, 10, 7, 0)
    assert steps.settle_deadline - steps.settle_after == timedelta(hours=24)


def test_weekly_drop() -> None:
    sunday = date(2026, 10, 4)
    out = specs(Timeframe.WEEKLY, sunday)
    assert len(out) == 6  # weight + 5 counts incl. workouts
    weight = out[0]
    assert (weight.window_start, weight.window_end) == (sunday, date(2026, 10, 11))
    assert weight.lock_at == local(2026, 10, 10, 22)  # Saturday night
    for count in out[1:]:
        assert (count.window_start, count.window_end) == (date(2026, 10, 5), date(2026, 10, 11))
        assert count.lock_at == local(2026, 10, 4, 22)  # the drop night
        assert len(count.correlation_keys) == 7
    assert out[1].title == "Steps, Mon Oct 5 to Sun Oct 11"


def test_monthly_drop_crosses_dst_and_month_lengths() -> None:
    out = specs(Timeframe.MONTHLY, date(2026, 10, 31), ("steps",))
    weight, steps = out
    assert (weight.window_start, weight.window_end) == (date(2026, 10, 31), date(2026, 11, 30))
    assert weight.lock_at == local(2026, 11, 29, 22)
    assert steps.lock_at == local(2026, 10, 31, 22)
    assert (steps.window_start, steps.window_end) == (date(2026, 11, 1), date(2026, 11, 30))
    assert steps.title == "Steps in November 2026"
    feb = specs(Timeframe.MONTHLY, date(2027, 1, 31), ())
    assert [s.window_end for s in feb] == [date(2027, 2, 28)]


def test_enabled_metrics_are_respected() -> None:
    assert [s.metric for s in specs(Timeframe.DAILY, date(2026, 10, 5), ("kcal",))] == [
        "weight",
        "kcal",
    ]
    weekly = specs(Timeframe.WEEKLY, date(2026, 10, 4), ("workouts",))
    assert [s.metric for s in weekly] == ["weight", "workouts"]


def test_dedupe_keys_are_canonical_and_distinct() -> None:
    a = specs(Timeframe.DAILY, date(2026, 10, 5))
    b = specs(Timeframe.DAILY, date(2026, 10, 5))
    assert [s.dedupe_key for s in a] == [s.dedupe_key for s in b]
    assert len({s.dedupe_key for s in a}) == len(a)
    assert a[0].dedupe_key == 'weight_change_ou:{"d0":"2026-10-05","d1":"2026-10-06"}'


def test_params_are_validated() -> None:
    with pytest.raises(ValueError, match="d1 must be after d0"):
        WeightChangeParams(d0=date(2026, 10, 5), d1=date(2026, 10, 5))
    with pytest.raises(ValueError, match="end must not be before start"):
        MetricTotalParams(metric="steps", start=date(2026, 10, 5), end=date(2026, 10, 4))
    with pytest.raises(ValueError):
        MetricTotalParams.model_validate({"metric": "sleep", "start": "2026-10-05", "end": "x"})


def test_registry_and_settle_stub() -> None:
    assert set(TEMPLATES) == {"weight_change_ou", "metric_total_ou"}
    spec = specs(Timeframe.DAILY, date(2026, 10, 5))[0]
    with pytest.raises(NotImplementedError):
        TEMPLATES[spec.template].settle(spec, [])


# ---- pricing ---------------------------------------------------------------------------


def test_weight_pricing_uses_the_start_weigh_in() -> None:
    day = date(2026, 10, 5)
    spec = specs(Timeframe.DAILY, day)[0]
    weigh_ins = {day - timedelta(days=k): 2000 + 2 * k for k in range(20)}
    pricing = WEIGHT_CHANGE_OU.price(spec, PricingData(as_of=day, weigh_ins=weigh_ins), "lb")
    assert pricing is not None
    assert pricing.model_inputs["start_known"] is True
    assert pricing.model_inputs["start_weight"] == 200.0
    assert pricing.odds_over is not None and pricing.odds_under is not None
    missing_start = {d: v for d, v in weigh_ins.items() if d != day}
    pricing = WEIGHT_CHANGE_OU.price(spec, PricingData(as_of=day, weigh_ins=missing_start), "lb")
    assert pricing is not None and pricing.model_inputs["start_known"] is False
    empty = WEIGHT_CHANGE_OU.price(spec, PricingData(as_of=day), "lb")
    assert empty is not None and empty.model_inputs["sigma_source"] == "prior"
    with pytest.raises(ValueError, match="start day"):
        WEIGHT_CHANGE_OU.price(spec, PricingData(as_of=day + timedelta(days=1)), "lb")


def test_count_pricing_uses_only_complete_days() -> None:
    day = date(2026, 10, 5)
    spec = specs(Timeframe.DAILY, day)[1]  # steps tomorrow
    history = {day - timedelta(days=k): 9000 for k in range(0, 30)}
    history[day] = 50  # today so far: incomplete, must be ignored
    data = PricingData(
        as_of=day,
        daily_totals={"steps": history},
        complete_through={"steps": day - timedelta(days=1)},
    )
    pricing = METRIC_TOTAL_OU.price(spec, data, "lb")
    assert pricing is not None
    assert pricing.line_x10 == 89995  # 9,000 -> O/U 8,999.5
    assert METRIC_TOTAL_OU.price(spec, PricingData(as_of=day), "lb") is None
    stale = PricingData(as_of=day, daily_totals={"steps": {day - timedelta(days=40): 9000}})
    assert METRIC_TOTAL_OU.price(spec, stale, "lb") is None


def test_workout_pricing_is_poisson() -> None:
    sunday = date(2026, 10, 4)
    spec = specs(Timeframe.WEEKLY, sunday, ("workouts",))[1]
    counts = {sunday - timedelta(days=k): 1 for k in range(1, 29, 2)}  # every other day
    pricing = METRIC_TOTAL_OU.price(
        spec, PricingData(as_of=sunday, daily_totals={"workouts": counts}), "lb"
    )
    assert pricing is not None
    assert pricing.model_inputs["model"] == "workouts_poisson"
    assert pricing.model_inputs["lambda"] == pytest.approx(3.5)
    assert pricing.line_x10 == 35
