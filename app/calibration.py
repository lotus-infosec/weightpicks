"""Line-engine calibration harness (STAGE04, BUILD_PLAN §3 Sprint 4).

Replays simulated weigh-ins and asks: when the engine says Over has probability p,
does Over hit about p of the time? Samples come from several seeds and from the main
line plus nearby lines, so every probability decile gets enough data to judge (a
single 365-day run leaves ~30 samples per decile, whose noise alone is ±8 points).
Outcomes are settled on integer tenths exactly like real settlement; exact ties push
and are excluded (D-013). Provisional (< 7 weigh-in) fits are excluded and counted.
The model priced is exactly `app.domain.lines` (D-029: 12-week sigma, level term).
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
from scipy.stats import norm
from scipy.stats import t as student_t

from app.domain.lines import SIGMA_WINDOW_DAYS, change_distribution, fit_weight, snap_half
from app.domain.schedule import in_weigh_in_window, local_date
from app.domain.units import Unit, grams_to_tenths
from app.providers.simulated import SimulatedProvider

ANCHOR = date(2026, 1, 1)
HORIZONS = (1, 7)
LINE_OFFSETS = (-2, -1, 0, 1, 2)
MIN_GRADED = 200
TOLERANCE = 0.05


@dataclass(frozen=True, slots=True)
class Decile:
    low: float
    n: int
    implied: float
    realised: float

    @property
    def graded(self) -> bool:
        return self.n >= MIN_GRADED

    @property
    def ok(self) -> bool:
        return not self.graded or abs(self.realised - self.implied) <= TOLERANCE


@dataclass(slots=True)
class CalibrationReport:
    preset: str
    days: int
    seeds: int
    tz: str
    unit: Unit
    samples: int = 0
    pushes: int = 0
    provisional_days: int = 0
    deciles: list[Decile] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(d.ok for d in self.deciles) and any(d.graded for d in self.deciles)

    @property
    def status(self) -> str:
        if not any(d.graded for d in self.deciles):
            return "INSUFFICIENT DATA"
        return "PASS" if self.passed else "FAIL"


def canonical_tenths(
    provider: SimulatedProvider, days: int, tz: ZoneInfo, unit: Unit
) -> dict[date, int]:
    """Earliest in-window weigh-in per local day, in tenths (the settlement value)."""
    start = datetime.combine(provider.anchor, datetime.min.time(), tzinfo=tz).astimezone(UTC)
    batch = provider.fetch_since(start - timedelta(seconds=1), start + timedelta(days=days))
    series: dict[date, int] = {}
    for w in sorted(batch.weigh_ins, key=lambda w: w.at):
        if in_weigh_in_window(w.at, tz):
            series.setdefault(local_date(w.at, tz), grams_to_tenths(w.grams, unit))
    return series


def collect(
    series: dict[date, int], *, unit: Unit, report: CalibrationReport
) -> tuple[list[float], list[bool]]:
    implied: list[float] = []
    over: list[bool] = []
    offsets = np.array(LINE_OFFSETS, dtype=float)
    for day in sorted(series):
        points = [
            ((d - day).days, series[d] / 10)
            for d in (day - timedelta(days=k) for k in range(SIGMA_WINDOW_DAYS))
            if d in series
        ]
        fit = fit_weight(points, unit)
        if fit.provisional:
            report.provisional_days += 1
            continue
        start = series[day]
        for h in HORIZONS:
            end = series.get(day + timedelta(days=h))
            if end is None:
                continue
            mu, sd = change_distribution(fit, h, start_weight=start / 10, unit=unit)
            lines = snap_half(mu) + offsets
            z = (lines - mu) / sd
            probs = norm.sf(z) if fit.df is None else student_t.sf(z, fit.df)
            change_x10 = end - start
            for line, p in zip(lines, probs, strict=True):
                line_x10 = round(line * 10)
                if change_x10 == line_x10:
                    report.pushes += 1
                    continue
                implied.append(float(p))
                over.append(change_x10 > line_x10)
    return implied, over


def run(preset: str, *, days: int, seeds: int, tz: ZoneInfo, unit: Unit) -> CalibrationReport:
    report = CalibrationReport(preset=preset, days=days, seeds=seeds, tz=tz.key, unit=unit)
    implied: list[float] = []
    over: list[bool] = []
    for seed in range(1, seeds + 1):
        provider = SimulatedProvider(preset=preset, seed=seed, anchor_date=ANCHOR, tz=tz)
        p, o = collect(canonical_tenths(provider, days, tz, unit), unit=unit, report=report)
        implied += p
        over += o
    report.samples = len(implied)
    report.deciles = deciles(implied, over)
    return report


def deciles(implied: Sequence[float], over: Sequence[bool]) -> list[Decile]:
    p = np.array(implied)
    o = np.array(over, dtype=float)
    bucket = np.minimum((p * 10).astype(int), 9)
    rows = []
    for k in range(10):
        mask = bucket == k
        n = int(mask.sum())
        rows.append(
            Decile(
                low=k / 10,
                n=n,
                implied=float(p[mask].mean()) if n else 0.0,
                realised=float(o[mask].mean()) if n else 0.0,
            )
        )
    return rows


def render_markdown(report: CalibrationReport) -> str:
    lines = [
        f"# Line-engine calibration — `{report.preset}`",
        "",
        "Generated by `wp calibrate`. Deterministic: rerunning with the same arguments "
        "reproduces this file exactly.",
        "",
        "| Setting | Value |",
        "| --- | --- |",
        f"| Preset | `{report.preset}` |",
        f"| Simulated days per seed | {report.days} |",
        f"| Seeds | 1-{report.seeds} |",
        f"| Markets | weight change, start known, horizons {', '.join(map(str, HORIZONS))} days |",
        f"| Lines priced per market | main line {', '.join(f'{o:+d}' for o in LINE_OFFSETS)} "
        f"{report.unit} |",
        f"| Time zone / unit | {report.tz} / {report.unit} |",
        f"| Samples | {report.samples:,} |",
        f"| Excluded: exact ties (push) | {report.pushes:,} |",
        f"| Excluded: provisional days (< 7 weigh-ins) | {report.provisional_days:,} |",
        f"| Graded decile | n ≥ {MIN_GRADED} and abs(realised - implied) ≤ {TOLERANCE:.0%} |",
        "",
        "| Implied P(Over) | n | Mean implied | Realised | Difference | OK |",
        "| --- | ---: | ---: | ---: | ---: | :---: |",
    ]
    for d in report.deciles:
        if d.n == 0:
            lines.append(f"| {d.low:.1f}-{d.low + 0.1:.1f} | 0 | — | — | — | — |")
            continue
        status = ("✅" if d.ok else "❌") if d.graded else "n/a"
        lines.append(
            f"| {d.low:.1f}-{d.low + 0.1:.1f} | {d.n:,} | {d.implied:.1%} | {d.realised:.1%} "
            f"| {(d.realised - d.implied) * 100:+.1f} pts | {status} |"
        )
    lines += ["", f"**Result: {report.status}**", ""]
    return "\n".join(lines)
