"""`wp report reconcile --days N`: what the engine used next to everything Garmin sent,
day by day, so the admin can check it by hand against the Garmin Connect app (STAGE10).
Read-only."""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import Connection, select

from app.models import Market, Observation, Settlement
from app.services.observations import canonical_weigh_ins, latest_complete_through


@dataclass(slots=True)
class DayReport:
    day: date
    weigh_ins: list[tuple[datetime, int, str, bool]] = field(default_factory=list)
    canonical: int | None = None  # tenths of the unit
    totals: dict[str, int] = field(default_factory=dict)
    workouts: int = 0
    settled: list[str] = field(default_factory=list)


def build(conn: Connection, today: date, days: int) -> list[DayReport]:
    first = today - timedelta(days=days - 1)
    reports = {first + timedelta(days=i): DayReport(first + timedelta(days=i)) for i in range(days)}
    for metric, day, at, value, source, in_window in conn.execute(
        select(
            Observation.metric,
            Observation.local_date,
            Observation.observed_at,
            Observation.value,
            Observation.source,
            Observation.in_window,
        )
        .where(Observation.local_date.between(first, today))
        .order_by(Observation.observed_at, Observation.id)
    ):
        report = reports[day]
        if metric == "weight":
            report.weigh_ins.append((at, value, source, bool(in_window)))
        elif metric == "activity":
            report.workouts += 1
        else:
            report.totals[metric] = value
    for c in canonical_weigh_ins(conn, first, today):
        reports[c.local_date].canonical = c.value
    for title, window_end, outcome in conn.execute(
        select(Market.title, Market.window_end, Settlement.outcome)
        .join(Settlement, Settlement.market_id == Market.id)
        .where(Market.window_end.between(first, today))
        .order_by(Market.id)
    ):
        reports[window_end].settled.append(f"{title}: {outcome.get('winner') or 'push'}")
    return [reports[d] for d in sorted(reports)]


def render(reports: list[DayReport], conn: Connection, tz: ZoneInfo, unit: str) -> str:
    def weight(tenths: int | None) -> str:
        return "-" if tenths is None else f"{tenths / 10:.1f} {unit}"

    lines = [f"complete through: {latest_complete_through(conn) or 'no successful sync yet'}"]
    for r in reversed(reports):
        lines.append(f"\n{r.day.isoformat()}  canonical weigh-in: {weight(r.canonical)}")
        for at, value, source, in_window in r.weigh_ins:
            flag = "in window" if in_window else "outside window"
            local = at.astimezone(tz).strftime("%H:%M")
            lines.append(f"  weigh-in {local} {weight(value)} ({source}, {flag})")
        if r.totals:
            lines.append("  " + ", ".join(f"{k} {v}" for k, v in sorted(r.totals.items())))
        if r.workouts:
            lines.append(f"  workouts (10+ min): {r.workouts}")
        lines += [f"  settled: {s}" for s in r.settled]
    return "\n".join(lines)
