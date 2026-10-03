from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from app.domain.schedule import in_weigh_in_window, local_date, next_local_midnight
from app.providers.base import COUNT_METRICS, WeighIn
from app.providers.simulated import PRESETS, SimulatedProvider

NY = ZoneInfo("America/New_York")
ANCHOR = date(2026, 10, 1)
START = datetime(2026, 10, 1, 0, 0, tzinfo=NY).astimezone(UTC)


def sim(preset: str = "steady-loser", seed: int = 42) -> SimulatedProvider:
    return SimulatedProvider(preset=preset, seed=seed, anchor_date=ANCHOR, tz=NY)


def canonical(weigh_ins: list[WeighIn]) -> dict[date, WeighIn]:
    best: dict[date, WeighIn] = {}
    for w in sorted(weigh_ins, key=lambda w: w.at):
        if in_weigh_in_window(w.at, NY):
            best.setdefault(local_date(w.at, NY), w)
    return best


def test_same_seed_same_data_and_different_seed_differs() -> None:
    end = START + timedelta(days=20)
    assert sim(seed=7).fetch_since(START, end) == sim(seed=7).fetch_since(START, end)
    assert sim(seed=7).fetch_since(START, end) != sim(seed=8).fetch_since(START, end)


def test_split_fetches_equal_one_fetch() -> None:
    provider = sim()
    whole = provider.fetch_since(START, START + timedelta(days=10))
    cut = START + timedelta(days=4, hours=7, minutes=13)
    first = provider.fetch_since(START, cut)
    second = provider.fetch_since(cut, START + timedelta(days=10))
    assert first.weigh_ins + second.weigh_ins == whole.weigh_ins
    assert first.daily_totals + second.daily_totals == whole.daily_totals
    assert first.activities + second.activities == whole.activities


def test_nothing_before_the_anchor_and_nothing_from_the_future() -> None:
    provider = sim()
    batch = provider.fetch_since(START - timedelta(days=30), START + timedelta(days=3))
    assert all(w.at >= START for w in batch.weigh_ins)
    now = START + timedelta(days=3)
    assert all(w.at <= now for w in batch.weigh_ins)
    assert all(a.at <= now for a in batch.activities)


def test_daily_totals_are_released_only_after_local_midnight() -> None:
    provider = sim()
    late_evening = datetime(2026, 10, 5, 23, 59, tzinfo=NY).astimezone(UTC)
    batch = provider.fetch_since(START, late_evening)
    assert {t.local_date for t in batch.daily_totals} <= {
        ANCHOR + timedelta(days=n) for n in range(4)
    }
    for total in batch.daily_totals:
        assert late_evening >= next_local_midnight(total.local_date, NY)


def test_a_year_of_data_has_realistic_rates() -> None:
    provider = sim()
    batch = provider.fetch_since(START, START + timedelta(days=365))
    days = 365
    canon = canonical(batch.weigh_ins)
    skip_rate = 1 - len(canon) / days
    manual_rate = sum(w.source == "manual" for w in canon.values()) / len(canon)
    preset = PRESETS["steady-loser"]
    assert abs(skip_rate - preset.skip_rate) < 0.06
    assert abs(manual_rate - preset.manual_rate) < 0.06
    # Some days have a second weigh-in; some weigh-ins fall outside the window.
    assert len(batch.weigh_ins) > len(canon) + 10
    assert any(not in_weigh_in_window(w.at, NY) for w in batch.weigh_ins)
    # Every count metric is present on most days, never negative; some days have no record.
    for metric in COUNT_METRICS:
        values = [t.value for t in batch.daily_totals if t.metric == metric]
        assert 300 < len(values) < days
        assert min(values) >= 0
    steps_by_weekday: dict[bool, list[int]] = {True: [], False: []}
    for t in batch.daily_totals:
        if t.metric == "steps":
            steps_by_weekday[t.local_date.weekday() >= 5].append(t.value)
    assert np.mean(steps_by_weekday[False]) > np.mean(steps_by_weekday[True])
    assert any(a.duration_min >= 10 for a in batch.activities)
    assert any(a.duration_min < 10 for a in batch.activities)


def test_steady_loser_trends_down_and_goal_preset_crosses_goal() -> None:
    canon = canonical(sim().fetch_since(START, START + timedelta(days=60)).weigh_ins)
    days = np.array([(d - ANCHOR).days for d in canon])
    grams = np.array([w.grams for w in canon.values()])
    slope = np.polyfit(days, grams, 1)[0]
    assert slope < -50  # grams per day

    goal = sim("goal-in-30-days")
    canon = canonical(goal.fetch_since(START, START + timedelta(days=35)).weigh_ins)
    goal_grams = PRESETS["goal-in-30-days"].goal_grams
    assert goal_grams is not None
    assert min(w.grams for w in canon.values()) <= goal_grams


@pytest.mark.parametrize("preset", sorted(PRESETS))
def test_every_preset_generates_plausible_weights(preset: str) -> None:
    batch = sim(preset).fetch_since(START, START + timedelta(days=90))
    grams = [w.grams for w in batch.weigh_ins]
    assert len(grams) > 30
    assert min(grams) > 40_000 and max(grams) < 200_000


def test_unknown_preset_rejected() -> None:
    with pytest.raises(ValueError, match="preset"):
        sim("marathon")


def test_complete_through_rules() -> None:
    provider = sim()
    # 10:59 on Oct 5: Oct 5's window is still open, Oct 4's daily totals are released.
    before_close = datetime(2026, 10, 5, 10, 59, tzinfo=NY).astimezone(UTC)
    ct = provider.complete_through(before_close)
    assert ct["weight"] == date(2026, 10, 4)
    assert ct["steps"] == date(2026, 10, 4)
    # 11:00: today's weight window has closed.
    assert provider.complete_through(before_close + timedelta(minutes=1))["weight"] == date(
        2026, 10, 5
    )
    # Never claim a day complete before its totals are released; claim it as soon as they are.
    for minutes in range(0, 180, 7):
        now = datetime(2026, 10, 6, 0, 0, tzinfo=NY).astimezone(UTC) + timedelta(minutes=minutes)
        done = provider.complete_through(now)["steps"]
        assert provider.release_time(done) <= now
        assert provider.release_time(done + timedelta(days=1)) > now
    assert set(provider.complete_through(before_close)) == {"weight", "workout", *COUNT_METRICS}
