"""Player stats view: served from `observations` only, so raw Garmin
history never leaves the server. The trend, band and projection are the line engine's
own fit, the same numbers behind the open weight lines. Read-only."""

from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from sqlalchemy import Connection, select

from app.domain.lines import SIGMA_WINDOW_DAYS, WINDOW_DAYS, change_distribution, fit_weight
from app.domain.units import Unit
from app.models import Observation, Season
from app.providers.base import WORKOUT_MIN_MINUTES
from app.services.ledger import active_season_id
from app.services.observations import (
    activity_counts,
    canonical_weigh_ins,
    latest_totals,
    workouts,
)

RANGES = (14, 30, 90)
PROJECTION_DAYS = 7
METRIC_LABELS = {
    "steps": "Steps",
    "active_minutes": "Active minutes",
    "intensity_minutes": "Intensity minutes",
    "kcal": "Active calories",
    "workouts": "Workouts",
}


def _days(first: date, last: date) -> list[date]:
    return [first + timedelta(days=i) for i in range((last - first).days + 1)]


def _round(x: float) -> float:
    return round(x, 2)


def build(
    conn: Connection, as_of: date, days: int, unit: Unit, metrics: tuple[str, ...]
) -> dict[str, Any]:
    days = days if days in RANGES else 30
    first = as_of - timedelta(days=days - 1)
    history = canonical_weigh_ins(conn, as_of - timedelta(days=SIGMA_WINDOW_DAYS - 1), as_of)
    by_day = {c.local_date: c for c in history}
    weigh_ins = [
        {"d": c.local_date.isoformat(), "v": c.value / 10, "s": c.source}
        for c in history
        if c.local_date >= first
    ]

    trend: list[dict[str, Any]] = []
    projection: list[dict[str, Any]] = []
    provisional = True
    points = [((c.local_date - as_of).days, c.value / 10) for c in history]
    if any(t > -WINDOW_DAYS for t, _ in points):
        fit = fit_weight(points, unit)
        provisional = fit.provisional
        for t in range(-(min(days, WINDOW_DAYS) - 1), 1):
            y = fit.a + fit.b * t
            trend.append(
                {
                    "d": (as_of + timedelta(days=t)).isoformat(),
                    "y": _round(y),
                    "lo": _round(y - fit.sigma),
                    "hi": _round(y + fit.sigma),
                }
            )
        for h in range(1, PROJECTION_DAYS + 1):
            _, sd = change_distribution(fit, h, start_weight=fit.a, unit=unit)
            y = fit.a + fit.b * h
            projection.append(
                {
                    "d": (as_of + timedelta(days=h)).isoformat(),
                    "y": _round(y),
                    "lo": _round(y - sd),
                    "hi": _round(y + sd),
                }
            )

    series = _metric_series(conn, first, as_of, metrics)
    return {
        "unit": unit,
        "days": days,
        "ranges": list(RANGES),
        "as_of": as_of.isoformat(),
        "labels": [d.isoformat() for d in _days(first, as_of + timedelta(days=PROJECTION_DAYS))],
        "weigh_ins": weigh_ins,
        "trend": trend,
        "projection": projection,
        "provisional": provisional,
        "metrics": series,
        "strip": _strip(conn, as_of, by_day),
    }


def _metric_series(
    conn: Connection, first: date, last: date, metrics: tuple[str, ...]
) -> list[dict[str, Any]]:
    wanted = [m for m in metrics if m in METRIC_LABELS]
    if not wanted:
        return []
    values: dict[str, dict[date, int]] = defaultdict(dict)
    count_metrics = [m for m in wanted if m != "workouts"]
    for row in latest_totals(conn, first - timedelta(days=6), last, count_metrics):
        values[row.metric][row.local_date] = row.value
    if "workouts" in wanted:
        steps_days = {
            d
            for (d,) in conn.execute(
                select(Observation.local_date)
                .where(
                    Observation.metric == "steps",
                    Observation.local_date.between(first - timedelta(days=6), last),
                )
                .distinct()
            )
        }
        counts: dict[date, int] = dict.fromkeys(steps_days, 0)  # a day with data and no workout
        counts |= activity_counts(conn, first - timedelta(days=6), last)
        values["workouts"] = counts
    out = []
    for metric in wanted:
        daily = values.get(metric, {})
        days = _days(first, last)
        avg = []
        for d in days:
            window = [daily[x] for x in _days(d - timedelta(days=6), d) if x in daily]
            avg.append(_round(sum(window) / len(window)) if window else None)
        item: dict[str, Any] = {
            "metric": metric,
            "label": METRIC_LABELS[metric],
            "values": [daily.get(d) for d in days],
            "avg7": avg,
            "labels": [d.isoformat() for d in days],
        }
        if metric == "workouts":  # issue #66: each workout's length, not a daily count
            item |= _workout_dots(conn, first, last)
        out.append(item)
    return out


def _workout_dots(conn: Connection, first: date, last: date) -> dict[str, Any]:
    """One dot per workout (day, minutes), plus what a bettor wants at a glance: how
    many count for markets (10+ minutes) and the typical length of those."""
    found = workouts(conn, first, last)
    counted = sorted(m for _, m in found if m >= WORKOUT_MIN_MINUTES)
    return {
        "dots": [{"x": d.isoformat(), "y": m} for d, m in found],
        "min_minutes": WORKOUT_MIN_MINUTES,
        "counted": len(counted),
        "median": counted[len(counted) // 2] if counted else None,
    }


def _strip(conn: Connection, as_of: date, by_day: dict[date, Any]) -> dict[str, Any]:
    if not by_day:
        return {"current": None, "change_7d": None, "to_goal": None, "streak": 0}
    latest_day = max(by_day)
    current = by_day[latest_day].value / 10
    week_ago = [d for d in by_day if d <= latest_day - timedelta(days=7)]
    change = current - by_day[max(week_ago)].value / 10 if week_ago else None
    goal = None
    season_id = active_season_id(conn)
    if season_id is not None:
        goal_x10 = conn.execute(
            select(Season.goal_weight_x10).where(Season.id == season_id)
        ).scalar_one_or_none()
        goal = None if goal_x10 is None else _round(abs(current - goal_x10 / 10))
    streak = 0
    day = as_of if as_of in by_day else as_of - timedelta(days=1)
    while day in by_day:
        streak += 1
        day -= timedelta(days=1)
    return {
        "current": current,
        "change_7d": None if change is None else _round(change),
        "to_goal": goal,
        "streak": streak,
    }
