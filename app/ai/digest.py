"""The AI digest and template menu, built by code (BUILD_PLAN §1.4.4 step 2; D-042).

Pure functions over a snapshot the service gathers. The menu holds the only parameter
values a proposal may use; `Menu.form` turns a proposal into the admin prop form, or
says why it is off the menu. No bettor names ever go into the digest.
"""

import math
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from typing import Any

from app.domain.lines import fit_weight
from app.domain.units import Unit

DEADLINE_DAYS = (2, 10)  # props: deadline 2-10 days from today
FUTURE_DAYS = (14, 60)
STREAK_EXTRA = (2, 7)  # streak n = current + 2 ... current + 7
ROUND_EVERY = 5  # a "round" milestone is a multiple of 5 units
NEAR_ROUND = 2.0  # within 2 units of a round milestone -> event trigger
STREAK_TRIGGER = 3
Z_TRIGGER = 1.5
NORMAL_DAYS = 28
METRIC_NAMES = {
    "steps": "steps",
    "active_minutes": "active minutes",
    "intensity_minutes": "intensity minutes",
    "kcal": "active calories",
    "workouts": "workouts",
}


@dataclass(frozen=True, slots=True)
class Snapshot:
    today: date
    unit: Unit
    direction: str  # season goal direction: "down" | "up"
    weigh_ins: Mapping[date, int]  # canonical, tenths, up to 84 days
    daily_totals: Mapping[str, Mapping[date, int]]
    metrics: tuple[str, ...]  # enabled count metrics
    notes: tuple[str, ...] = ()
    storylines: tuple[str, ...] = ()


def streak(weigh_ins: Mapping[date, int], today: date, kind: str) -> int:
    """Length of the streak ending today (same rule as the admin prop form)."""
    run, day = 0, today
    while day in weigh_ins:
        before = weigh_ins.get(day - timedelta(days=1))
        if kind == "down" and (before is None or weigh_ins[day] >= before):
            break
        run += 1
        day -= timedelta(days=1)
    return run


@dataclass(frozen=True, slots=True)
class Menu:
    """Allowed values per template. Dates are inclusive (first, last) ranges.

    Weight props (milestone, streak) are offered as explicit options once the service
    has priced the candidates (`with_options`): only combinations the engine will
    actually offer reach the model. Metric props and futures use plain ranges."""

    today: date
    thresholds: tuple[float, ...] = ()
    deadlines: tuple[date, date] | None = None
    streak_kinds: tuple[str, ...] = ()
    streak_current: Mapping[str, int] = field(default_factory=dict)
    metrics: tuple[str, ...] = ()
    future_days: tuple[date, date] | None = None
    milestone_options: tuple[dict[str, str], ...] | None = None
    streak_options: tuple[dict[str, str], ...] | None = None

    @property
    def streak_n(self) -> dict[str, tuple[int, int]]:
        return {
            k: (max(2, c + STREAK_EXTRA[0]), c + STREAK_EXTRA[1])
            for k, c in self.streak_current.items()
        }

    @property
    def templates(self) -> tuple[str, ...]:
        names = []
        if (
            self.milestone_options
            if self.milestone_options is not None
            else (self.thresholds and self.deadlines)
        ):
            names.append("milestone_by")
        if (
            self.streak_options
            if self.streak_options is not None
            else (self.streak_kinds and self.deadlines)
        ):
            names.append("streak_reaches")
        if self.metrics:
            names.append("beat_last_week")
        if self.future_days:
            names.append("future_total_change")
        return tuple(names)

    def candidates(self, template: str) -> list[dict[str, str]]:
        """Forms worth pricing for a weight prop: a few deadlines per threshold, and per
        streak length a deadline just reachable or with a little slack."""
        if self.deadlines is None:
            return []
        first, last = self.deadlines
        out: list[dict[str, str]] = []
        if template == "milestone_by":
            steps = sorted({first, self.today + timedelta(4), self.today + timedelta(7), last})
            for t in self.thresholds:
                out += [{"threshold": f"{t:g}", "deadline": d.isoformat()} for d in steps]
        elif template == "streak_reaches":
            for kind in self.streak_kinds:
                current = self.streak_current[kind]
                lo, hi = self.streak_n[kind]
                for n in range(lo, hi + 1):
                    for slack in (0, 2):
                        # The window starts tomorrow, so n - current days are needed.
                        day = self.today + timedelta(days=n - current + slack)
                        if first <= day <= last:
                            out.append({"kind": kind, "n": str(n), "deadline": day.isoformat()})
        return out

    def with_options(
        self, milestone: Sequence[dict[str, str]], streak: Sequence[dict[str, str]]
    ) -> "Menu":
        return replace(self, milestone_options=tuple(milestone), streak_options=tuple(streak))

    def as_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        names = self.templates
        if "milestone_by" in names:
            if self.milestone_options is not None:
                out["milestone_by"] = {
                    "options": [
                        {"threshold": float(o["threshold"]), "deadline": o["deadline"]}
                        for o in self.milestone_options
                    ]
                }
            elif self.deadlines:
                out["milestone_by"] = {
                    "threshold": list(self.thresholds),
                    "deadline": _range(self.deadlines),
                }
        if "streak_reaches" in names:
            if self.streak_options is not None:
                out["streak_reaches"] = {
                    "options": [
                        {"kind": o["kind"], "n": int(o["n"]), "deadline": o["deadline"]}
                        for o in self.streak_options
                    ]
                }
            elif self.deadlines:
                out["streak_reaches"] = {
                    "kind": list(self.streak_kinds),
                    "n": {k: {"min": lo, "max": hi} for k, (lo, hi) in self.streak_n.items()},
                    "deadline": _range(self.deadlines),
                }
        if self.metrics:
            out["beat_last_week"] = {"metric": list(self.metrics)}
        if self.future_days:
            out["future_total_change"] = {"day": _range(self.future_days)}
        return out

    def form(self, template: str, params: Mapping[str, Any]) -> dict[str, str] | str:
        """The admin prop form for this proposal, or the reason it is off the menu."""
        if template not in self.templates:
            return "off_menu"
        try:
            if template == "milestone_by":
                threshold = float(params["threshold"])
                if threshold not in self.thresholds:
                    return "threshold_out_of_range"
                deadline = _date_in(params["deadline"], self.deadlines)
                if deadline is None:
                    return "deadline_out_of_range"
                form = {"threshold": f"{threshold:g}", "deadline": deadline.isoformat()}
                if self.milestone_options is not None and form not in self.milestone_options:
                    return "not_an_option"
                return form
            if template == "streak_reaches":
                kind = str(params["kind"])
                if kind not in self.streak_kinds:
                    return "kind_out_of_range"
                n = params["n"]
                lo, hi = self.streak_n[kind]
                if not isinstance(n, int) or isinstance(n, bool) or not lo <= n <= hi:
                    return "n_out_of_range"
                deadline = _date_in(params["deadline"], self.deadlines)
                if deadline is None:
                    return "deadline_out_of_range"
                form = {"kind": kind, "n": str(n), "deadline": deadline.isoformat()}
                if self.streak_options is not None and form not in self.streak_options:
                    return "not_an_option"
                return form
            if template == "beat_last_week":
                metric = str(params["metric"])
                if metric not in self.metrics:
                    return "metric_out_of_range"
                return {"metric": metric}
            day = _date_in(params["day"], self.future_days)
            if day is None:
                return "day_out_of_range"
            return {"day": day.isoformat()}
        except (KeyError, TypeError, ValueError):
            return "bad_params"


def _range(span: tuple[date, date]) -> dict[str, str]:
    return {"min": span[0].isoformat(), "max": span[1].isoformat()}


def _date_in(raw: Any, span: tuple[date, date] | None) -> date | None:
    if span is None or not isinstance(raw, str):
        return None
    value = date.fromisoformat(raw)
    return value if span[0] <= value <= span[1] else None


@dataclass(frozen=True, slots=True)
class Digest:
    data: dict[str, Any]
    menu: Menu
    triggers: tuple[str, ...]

    def payload(self, menu: Menu | None = None) -> dict[str, Any]:
        """The JSON the model sees: the data plus the (priced) menu."""
        return self.data | {"menu": (menu or self.menu).as_json()}


def _yesterday_vs_normal(
    totals: Mapping[date, int], yesterday: date
) -> tuple[int, float, float] | None:
    value = totals.get(yesterday)
    history = [
        v for d, v in totals.items() if timedelta(0) < yesterday - d <= timedelta(NORMAL_DAYS)
    ]
    if value is None or len(history) < 7:
        return None
    mean = statistics.fmean(history)
    sd = statistics.pstdev(history) or max(1.0, 0.1 * mean)
    return value, mean, (value - mean) / sd


def build(snap: Snapshot) -> Digest:
    today, unit = snap.today, snap.unit
    data: dict[str, Any] = {"as_of": today.isoformat(), "unit": unit}
    triggers: list[str] = []
    menu_args: dict[str, Any] = {"today": today}
    deadlines = (today + timedelta(DEADLINE_DAYS[0]), today + timedelta(DEADLINE_DAYS[1]))

    recent = [(d, v) for d, v in snap.weigh_ins.items() if today - d < timedelta(days=84)]
    in_window = [p for p in recent if today - p[0] < timedelta(days=14)]
    if in_window:
        fit = fit_weight([((d - today).days, v / 10) for d, v in recent], unit)
        latest_day = max(d for d, _ in in_window)
        latest = snap.weigh_ins[latest_day] / 10
        down = snap.direction != "up"
        level = fit.a
        if down:
            base = math.floor(level)
            thresholds = tuple(float(base - k) for k in (1, 2, 3))
            next_round = math.floor((latest - 1e-9) / ROUND_EVERY) * ROUND_EVERY
        else:
            base = math.ceil(level)
            thresholds = tuple(float(base + k) for k in (1, 2, 3))
            next_round = math.ceil((latest + 1e-9) / ROUND_EVERY) * ROUND_EVERY
        distance = abs(latest - next_round)
        data["weight"] = {
            "latest": round(latest, 1),
            "latest_day": latest_day.isoformat(),
            "trend_per_week": round(fit.b * 7, 2),
            "goal_direction": "down" if down else "up",
            "next_round_milestone": next_round,
            "distance_to_round": round(distance, 1),
            "provisional": fit.provisional,
        }
        if distance <= NEAR_ROUND:
            triggers.append(f"within {distance:.1f} {unit} of {next_round}")
        if not fit.provisional:
            menu_args |= {
                "thresholds": thresholds,
                "deadlines": deadlines,
                "future_days": (
                    today + timedelta(FUTURE_DAYS[0]),
                    today + timedelta(FUTURE_DAYS[1]),
                ),
            }
    streaks = {k: streak(snap.weigh_ins, today, k) for k in ("weigh_in", "down")}
    data["streaks"] = streaks
    if "deadlines" in menu_args:
        kinds = tuple(k for k in ("weigh_in", "down") if k == "weigh_in" or today in snap.weigh_ins)
        menu_args |= {"streak_kinds": kinds, "streak_current": {k: streaks[k] for k in kinds}}
    for kind, length in streaks.items():
        if length >= STREAK_TRIGGER:
            label = "weigh-in" if kind == "weigh_in" else "lower-each-day"
            triggers.append(f"{length}-day {label} streak")

    yesterday = today - timedelta(days=1)
    rows = []
    offered = []
    for metric in snap.metrics:
        totals = snap.daily_totals.get(metric, {})
        compared = _yesterday_vs_normal(totals, yesterday)
        if len([d for d in totals if today - d <= timedelta(14)]) >= 7:
            offered.append(metric)
        if compared is None:
            continue
        value, mean, z = compared
        rows.append(
            {
                "metric": METRIC_NAMES.get(metric, metric),
                "yesterday": value,
                "normal": round(mean),
                "z": round(z, 2),
            }
        )
        if abs(z) > Z_TRIGGER:
            side = "above" if z > 0 else "below"
            triggers.append(f"{METRIC_NAMES.get(metric, metric)} {abs(z):.1f} SD {side} normal")
    menu_args["metrics"] = tuple(offered)
    data["yesterday_vs_normal"] = rows
    data["admin_notes"] = list(snap.notes)
    data["storylines"] = list(snap.storylines)
    data["triggers"] = triggers
    menu = Menu(**menu_args)
    return Digest(data, menu, tuple(triggers))


def crowd_storylines(
    splits: Sequence[tuple[str, int, int]], streaks: Sequence[tuple[str, int]]
) -> tuple[str, ...]:
    """Neutral, nameless storylines. `splits`: (market title, stake on side A, stake on
    side B) with sides named in the title order; `streaks`: (result, length) per bettor."""
    lines: list[str] = []
    for title, a, b in splits:
        total = a + b
        if total > 0 and max(a, b) / total >= 0.6:
            share = round(100 * max(a, b) / total)
            side = "Over/Yes" if a >= b else "Under/No"
            lines.append(f"The crowd is {share}% on {side} for '{title}'.")
    best = max(streaks, key=lambda s: s[1], default=None)
    if best is not None and best[1] >= 3:
        verb = "won" if best[0] == "won" else "lost"
        lines.append(f"One bettor has {verb} {best[1]} bets in a row.")
    return tuple(lines[:4])
