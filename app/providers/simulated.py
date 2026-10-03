"""Seeded, Garmin-shaped synthetic data (BUILD_PLAN §2.7). Dev and tests only.

Every local day draws from its own generator seeded by (seed, day, stream), so any
slice of time reproduces exactly no matter how it is fetched. All values are
synthetic: no real health data is ever used.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import numpy as np

from app.domain.schedule import local_date, next_local_midnight, window_close
from app.providers.base import (
    COUNT_METRICS,
    Activity,
    Batch,
    DailyTotal,
    WeighIn,
    WeighInSource,
)

GRAMS_PER_TENTH_LB = 45.359237
_DAY_OFFSET = 1_000_000  # keeps generator seeds non-negative for any date
_STREAM_WEIGHT, _STREAM_COUNTS, _STREAM_ACTIVITY = 1, 2, 3
_ACTIVITY_KINDS = ("walking", "running", "cycling", "strength")


@dataclass(frozen=True, slots=True)
class Preset:
    start_grams: int
    slope_grams_per_day: float
    sigma_grams: float
    skip_rate: float
    manual_rate: float
    second_weigh_in_rate: float = 0.12
    evening_rate: float = 0.05
    # Piecewise trend: after `bend_day` the slope becomes `slope_after`.
    bend_day: int | None = None
    slope_after: float = 0.0
    goal_grams: int | None = None

    def level(self, day: int) -> float:
        if self.bend_day is None or day <= self.bend_day:
            return self.start_grams + self.slope_grams_per_day * day
        bend = self.start_grams + self.slope_grams_per_day * self.bend_day
        return bend + self.slope_after * (day - self.bend_day)


PRESETS: dict[str, Preset] = {
    "steady-loser": Preset(104_000, -90, 450, skip_rate=0.12, manual_rate=0.15),
    "plateau": Preset(101_000, -90, 450, skip_rate=0.12, manual_rate=0.15, bend_day=21),
    "rebound": Preset(
        103_000, -110, 500, skip_rate=0.15, manual_rate=0.15, bend_day=30, slope_after=60
    ),
    "chaotic": Preset(99_000, -40, 1_100, skip_rate=0.30, manual_rate=0.30, evening_rate=0.15),
    "goal-in-30-days": Preset(
        92_000, -150, 400, skip_rate=0.10, manual_rate=0.10, goal_grams=87_500
    ),
}


@dataclass(frozen=True, slots=True)
class _Day:
    weigh_ins: tuple[WeighIn, ...]
    totals: tuple[DailyTotal, ...]
    totals_released_at: datetime
    activities: tuple[tuple[Activity, datetime], ...]  # (activity, available at)


class SimulatedProvider:
    name = "simulated"

    def __init__(self, *, preset: str, seed: int, anchor_date: date, tz: ZoneInfo) -> None:
        if preset not in PRESETS:
            raise ValueError(f"unknown preset {preset!r}; choose from {sorted(PRESETS)}")
        self.preset_name = preset
        self.preset = PRESETS[preset]
        self.seed = seed
        self.anchor = anchor_date
        self.tz = tz
        self._cache: dict[date, _Day] = {}

    # ---- public protocol -------------------------------------------------------------

    def fetch_since(self, since: datetime, now: datetime) -> Batch:
        batch = Batch()
        first = max(self.anchor, local_date(since, self.tz) - timedelta(days=1))
        for offset in range((local_date(now, self.tz) - first).days + 1):
            day = self._day(first + timedelta(days=offset))
            batch.weigh_ins.extend(w for w in day.weigh_ins if since < w.at <= now)
            if since < day.totals_released_at <= now:
                batch.daily_totals.extend(day.totals)
            batch.activities.extend(a for a, ready in day.activities if since < ready <= now)
        return batch

    def complete_through(self, now: datetime) -> dict[str, date]:
        today = local_date(now, self.tz)
        weight = today if now >= window_close(today, self.tz) else today - timedelta(days=1)
        counts = today - timedelta(days=1)
        if self.release_time(counts) > now:
            counts -= timedelta(days=1)
        floor = self.anchor - timedelta(days=1)
        result = {"weight": max(weight, floor), "workout": max(counts, floor)}
        result.update({m: max(counts, floor) for m in COUNT_METRICS})
        return result

    def release_time(self, day: date) -> datetime:
        """When `day`'s daily totals (and its completeness) become visible."""
        return self._day(day).totals_released_at

    # ---- generation -----------------------------------------------------------------

    def _rng(self, day: date, stream: int) -> np.random.Generator:
        return np.random.default_rng([self.seed, (day - self.anchor).days + _DAY_OFFSET, stream])

    def _at(self, day: date, minutes_after_midnight: float) -> datetime:
        local = datetime.combine(day, time(0), tzinfo=self.tz) + timedelta(
            minutes=int(minutes_after_midnight)
        )
        # Re-attach the zone so the wall-clock time is resolved for that date (DST-safe).
        return local.replace(tzinfo=self.tz).astimezone(UTC)

    def _day(self, day: date) -> _Day:
        cached = self._cache.get(day)
        if cached is None:
            cached = self._cache[day] = self._generate(day)
        return cached

    def _generate(self, day: date) -> _Day:
        if day < self.anchor:
            midnight = next_local_midnight(day, self.tz)
            return _Day((), (), midnight, ())
        return _Day(
            self._weigh_ins(day),
            *self._totals(day),
            self._activities(day),
        )

    def _weigh_ins(self, day: date) -> tuple[WeighIn, ...]:
        p, rng = self.preset, self._rng(day, _STREAM_WEIGHT)
        level = p.level((day - self.anchor).days)
        skipped, manual = rng.random() < p.skip_rate, rng.random() < p.manual_rate
        minute = float(np.clip(rng.normal(6.75 * 60, 45), 4 * 60, 10 * 60 + 55))
        noise = rng.normal(0, p.sigma_grams)
        second, second_gap, second_noise = (
            rng.random() < p.second_weigh_in_rate,
            rng.integers(20, 90),
            rng.normal(0, 150),
        )
        evening, evening_minute = rng.random() < p.evening_rate, rng.integers(19 * 60, 22 * 60)

        out: list[WeighIn] = []
        if not skipped:
            grams = level + noise
            if manual:  # typed in by hand to 0.1 lb
                grams = round(grams / GRAMS_PER_TENTH_LB) * GRAMS_PER_TENTH_LB
            source: WeighInSource = "manual" if manual else "scale"
            out.append(WeighIn(f"sim:w:{day}:1", self._at(day, minute), round(grams), source))
            if second:
                out.append(
                    WeighIn(
                        f"sim:w:{day}:2",
                        self._at(day, minute + int(second_gap)),
                        round(level + noise + second_noise),
                        "scale",
                    )
                )
        if evening:
            out.append(
                WeighIn(
                    f"sim:w:{day}:e",
                    self._at(day, int(evening_minute)),
                    round(level + 700 + noise),
                    "scale",
                )
            )
        return tuple(sorted(out, key=lambda w: w.at))

    def _totals(self, day: date) -> tuple[tuple[DailyTotal, ...], datetime]:
        rng = self._rng(day, _STREAM_COUNTS)
        released = next_local_midnight(day, self.tz) + timedelta(minutes=int(rng.integers(5, 120)))
        if rng.random() < 0.03:  # watch not worn: no record at all that day
            return (), released
        weekend = day.weekday() >= 5
        steps = max(300, round(rng.normal(6_500 if weekend else 8_500, 1_800)))
        values = {
            "steps": steps,
            "active_minutes": max(0, round(steps / 180 + rng.normal(0, 8))),
            "intensity_minutes": max(0, round(rng.normal(22, 12))),
            "kcal": max(0, round(steps * 0.045 + rng.normal(0, 60))),
        }
        totals = tuple(
            DailyTotal(f"sim:{metric}:{day}", day, metric, values[metric])
            for metric in COUNT_METRICS
        )
        return totals, released

    def _activities(self, day: date) -> tuple[tuple[Activity, datetime], ...]:
        rng = self._rng(day, _STREAM_ACTIVITY)
        out = []
        for n in range(int(rng.poisson(0.6))):
            start = self._at(day, float(rng.integers(6 * 60, 20 * 60)))
            duration = int(rng.integers(5, 9)) if rng.random() < 0.15 else int(rng.integers(15, 70))
            kind = _ACTIVITY_KINDS[int(rng.integers(len(_ACTIVITY_KINDS)))]
            activity = Activity(f"sim:a:{day}:{n}", start, duration, kind)
            out.append((activity, start + timedelta(minutes=duration)))
        return tuple(out)
