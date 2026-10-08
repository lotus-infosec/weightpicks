"""Stat-update ("hype") posts.

Only while the `ai_hype` flag is on. Code detects up to 4 stat events a day and writes
the template text; Workers AI (fp8-fast) may rewrite the wording. The rewrite must keep
every number, stay short and pass the text checks, or the template is posted instead.
Queued as `hype` outbox rows, one per event per day.
"""

import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import httpx
import structlog
from sqlalchemy import Connection, Engine, select

from app.ai import digest, runs
from app.ai.client import from_store
from app.ai.text import problem
from app.core.clock import Clock
from app.core.config import Settings
from app.core.db import immediate
from app.domain.schedule import local_date
from app.models import OutboxMessage, Season
from app.services import instance
from app.services.ai_props import bettor_names, snapshot
from app.services.instance import InstanceConfig
from app.services.ledger import active_season_id
from app.services.observations import canonical_weigh_ins
from app.services.outbox import Category, enqueue

log = structlog.get_logger()

PROMPT_VERSION = "hype_v1"
MAX_EVENTS = 4
MAX_TOKENS = 120
MAX_CHARS = 280
STREAK_MARKS = frozenset({3, 5, 7, 10, 14, 21, 30, 45, 60, 90, 100})
_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")


@dataclass(frozen=True, slots=True)
class Event:
    key: str
    text: str


def events(conn: Connection, config: InstanceConfig, today: date, now: datetime) -> list[Event]:
    """Today's stat events, most notable first, at most MAX_EVENTS."""
    snap = snapshot(conn, config, today, now)
    unit = config.unit
    found: list[Event] = []
    w = snap.weigh_ins
    if today in w:
        value = w[today] / 10
        earlier = any(d < today for d in w)
        season_id = active_season_id(conn)
        started = (
            conn.execute(select(Season.started_at).where(Season.id == season_id)).scalar_one()
            if season_id
            else None
        )
        if started is not None:
            first_day = local_date(started, config.tz)
            season = [c.value for c in canonical_weigh_ins(conn, first_day, today - timedelta(1))]
            down = snap.direction != "up"
            if season and (value * 10 < min(season) if down else value * 10 > max(season)):
                word = "low" if down else "high"
                found.append(Event("season_best", f"New season {word}: {value:.1f} {unit} today."))
        if earlier:
            previous = w[max(d for d in w if d < today)] / 10
            step = digest.ROUND_EVERY
            if snap.direction != "up":  # crossed below a multiple of 5 since the last weigh-in
                mark = math.ceil(value / step) * step if value % step else value + step
                if value < mark <= previous:
                    found.append(
                        Event(f"milestone_{mark:g}", f"Under {mark:g} {unit}: {value:.1f} today.")
                    )
            else:
                mark = math.floor(value / step) * step
                if previous < mark <= value:
                    found.append(
                        Event(f"milestone_{mark:g}", f"Reached {mark:g} {unit}: {value:.1f} today.")
                    )
    for kind, label in (("weigh_in", "weigh-in"), ("down", "lower-each-day")):
        length = digest.streak(w, today, kind)
        if length in STREAK_MARKS:
            found.append(Event(f"streak_{kind}", f"{length}-day {label} streak and counting."))
    built = digest.build(snap)
    for row in built.data.get("yesterday_vs_normal", []):
        if abs(row["z"]) > digest.Z_TRIGGER:
            side = "above" if row["z"] > 0 else "below"
            found.append(
                Event(
                    f"metric_{row['metric'].replace(' ', '_')}",
                    f"Yesterday: {row['yesterday']:,} {row['metric']}, well {side} the usual "
                    f"{row['normal']:,}.",
                )
            )
    return found[:MAX_EVENTS]


def _numbers(text: str) -> set[str]:
    return {n.replace(",", "") for n in _NUMBER.findall(text)}


def acceptable(rewrite: str, template: str, names: list[str]) -> bool:
    text = rewrite.strip()
    return (
        0 < len(text) <= MAX_CHARS
        and problem(text, names) is None
        and _numbers(template) <= _numbers(text)
    )


def run(
    engine: Engine,
    clock: Clock,
    real_clock: Clock,
    settings: Settings,
    *,
    transport: httpx.BaseTransport | None = None,
) -> int:
    """Queue today's hype posts; returns how many were queued."""
    now = clock.now()
    with engine.connect() as conn:
        config = instance.read(conn)
        if config is None or not config.flags.get("ai_hype"):
            return 0
        if instance.current_state(conn) == instance.FROZEN:
            return 0
        today = local_date(now, config.tz)
        todo = [
            e
            for e in events(conn, config, today, now)
            if conn.execute(
                select(OutboxMessage.id).where(OutboxMessage.dedupe_key == _key(today, e))
            ).first()
            is None
        ]
        client = from_store(conn, settings, transport) if todo else None
        names = bettor_names(conn)
    if todo and client is None:
        runs.record_skip(engine, real_clock, "hype", "no_token")
    queued = 0
    for event in todo:
        text, by_ai = event.text, False
        if client is not None:
            outcome = runs.call(
                engine,
                real_clock,
                client,
                kind="hype",
                model=settings.ai_model_text,
                prompt_version=PROMPT_VERSION,
                messages=[
                    {"role": "system", "content": runs.prompt(PROMPT_VERSION)},
                    {"role": "user", "content": event.text},
                ],
                max_tokens=MAX_TOKENS,
                cap=settings.ai_daily_neuron_cap,
            )
            rewrite = outcome.result.data if outcome.result is not None else None
            if isinstance(rewrite, str) and acceptable(rewrite, event.text, names):
                text, by_ai = rewrite.strip(), True
            elif outcome.result is not None:
                runs.finish(engine, outcome.run_id, errors=[{"reason": "rewrite_rejected"}])
        with immediate(engine) as conn:
            queued += enqueue(
                conn,
                clock,
                category=Category.HYPE,
                payload={"title": "Stat update", "text": text, "ai": by_ai, "event": event.key},
                dedupe_key=_key(today, event),
            )
    if todo:
        log.info("ai_hype_run", day=today.isoformat(), events=[e.key for e in todo], queued=queued)
    return queued


def _key(day: date, event: Event) -> str:
    return f"hype:{day.isoformat()}:{event.key}"
