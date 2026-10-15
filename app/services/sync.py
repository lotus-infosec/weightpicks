"""Provider sync and ingest.

The provider is called outside any database transaction (the real GarminDB sync is a
slow subprocess); only the ingest itself is one short BEGIN IMMEDIATE write.
Observations are inserted with ON CONFLICT DO NOTHING on (metric, source_ref), so
overlapping fetches and replays never duplicate data. A daily total Garmin now reports
differently from the stored one (the watch synced more of that day) is stored as a
correction: a new row whose ref names this run; readers take the newest (issue #57).
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import Connection, Engine, bindparam, func, select, update
from sqlalchemy.dialects.sqlite import insert

from app.core.clock import Clock
from app.core.config import Settings
from app.core.db import immediate
from app.domain.markets import Schedule
from app.domain.schedule import in_weigh_in_window, local_date, next_local_midnight
from app.domain.units import Unit, grams_to_tenths
from app.models import Observation, SyncRun
from app.providers.base import COUNT_METRICS, Batch, DataProvider
from app.providers.garmindb import GarminDBProvider
from app.providers.garmindb_runner import GarminDBRunner, GarminRunError
from app.providers.simulated import SimulatedProvider
from app.services import sim
from app.services.ledger import active_season_id
from app.services.outbox import Category, enqueue

log = structlog.get_logger()
FETCH_OVERLAP = timedelta(days=1)
STALE_AFTER = timedelta(hours=36)
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
MAX_ERROR_LENGTH = 2000


class ProviderUnavailable(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SyncResult:
    run_id: int
    status: str  # ok | failed
    rows_new: int
    error: str | None = None
    rows_corrected: int = 0  # daily totals stored again with a new value


def make_provider(
    settings: Settings,
    engine: Engine,
    clock: Clock,
    cached: DataProvider | None = None,
    *,
    tz: ZoneInfo | None = None,
    schedule: Schedule | None = None,
) -> DataProvider:
    """The configured provider. A cached simulator is reused while its config is unchanged
    (keeping its per-day data cache); a reseed produces a fresh one. The schedule carries
    the watch sync cutoff and grace period (issue #57)."""
    if settings.data_provider == "simulated":
        with engine.connect() as conn:  # no write lock once the state exists
            state = sim.read_state(conn)
        if state is None:
            with immediate(engine) as conn:
                state = sim.ensure_state(
                    conn, clock, seed=settings.sim_seed, tz_name=settings.wp_timezone
                )
        if isinstance(cached, SimulatedProvider) and (
            cached.preset_name,
            cached.seed,
            cached.anchor,
        ) == (state.preset, state.seed, state.anchor_date):
            return cached
        return SimulatedProvider(
            preset=state.preset,
            seed=state.seed,
            anchor_date=state.anchor_date,
            tz=tz or settings.tz,
        )
    zone = tz or settings.tz
    sched = schedule or Schedule()
    cutoff, grace = sched.watch_sync_cutoff, timedelta(hours=sched.watch_sync_grace_hours)
    if isinstance(cached, GarminDBProvider) and (cached.tz, cached.cutoff, cached.grace) == (
        zone,
        cutoff,
        grace,
    ):
        return cached
    if not (settings.garmin_home / ".GarminDb" / "garmin_tokens.json").is_file():
        raise ProviderUnavailable("no Garmin token yet: run garmin-login once")
    runner = GarminDBRunner(settings.garmin_home, settings.garmindb_python, zone.key)
    return GarminDBProvider(settings.garmin_home, zone, runner, cutoff=cutoff, grace=grace)


def _rows(
    batch: Batch, provider: str, run_id: int, tz: ZoneInfo, unit: Unit
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = [
        {
            "metric": "weight",
            "local_date": local_date(w.at, tz),
            "observed_at": w.at,
            "value": grams_to_tenths(w.grams, unit),
            "raw_value": w.grams,
            "source": w.source,
            "source_ref": w.ref,
            "in_window": in_weigh_in_window(w.at, tz),
            "sync_run_id": run_id,
        }
        for w in batch.weigh_ins
    ]
    rows += [
        {
            "metric": t.metric,
            "local_date": t.local_date,
            "observed_at": next_local_midnight(t.local_date, tz),  # end of the counted day
            "value": t.value,
            "raw_value": None,
            "source": provider,
            "source_ref": t.ref,
            "in_window": None,
            "sync_run_id": run_id,
        }
        for t in batch.daily_totals
    ]
    rows += [
        {
            "metric": "activity",
            "local_date": local_date(a.at, tz),
            "observed_at": a.at,
            "value": a.duration_min,
            "raw_value": None,
            "source": provider,
            "source_ref": a.ref,
            "in_window": None,
            "sync_run_id": run_id,
        }
        for a in batch.activities
    ]
    return rows


def run_sync(
    engine: Engine,
    clock: Clock,
    provider: DataProvider,
    *,
    tz: ZoneInfo,
    unit: Unit,
    deep_days: int | None = None,
) -> SyncResult:
    """One sync and ingest. `deep_days` (the admin's Sync now, issue #61) re-reads that
    many days: missing records are added and changed daily totals corrected."""
    now = clock.now()
    with immediate(engine) as conn:
        last_ok = conn.execute(
            select(func.max(SyncRun.started_at)).where(
                SyncRun.provider == provider.name, SyncRun.status == "ok"
            )
        ).scalar_one_or_none()
        run_id = conn.execute(
            insert(SyncRun)
            .values(provider=provider.name, started_at=now, status="running", rows_new=0)
            .returning(SyncRun.id)
        ).scalar_one()
    if last_ok is not None and last_ok.tzinfo is None:  # func.max() bypasses UTCDateTime
        last_ok = last_ok.replace(tzinfo=UTC)
    since = last_ok - FETCH_OVERLAP if last_ok is not None else EPOCH
    if deep_days:
        since = min(since, now - timedelta(days=deep_days))

    try:
        batch = provider.fetch_since(since, now, days=deep_days)
        complete = provider.complete_through(now)
    except Exception as exc:
        # GarminRunError messages are already scrubbed; anything else is shown by type.
        error = (str(exc) if isinstance(exc, GarminRunError) else repr(exc))[:MAX_ERROR_LENGTH]
        return _fail(engine, clock, provider.name, run_id, error)

    rows = _rows(batch, provider.name, run_id, tz, unit)
    with immediate(engine) as conn:
        rows, corrected = _corrections(conn, rows, run_id)
        new_ids = (
            conn.execute(
                insert(Observation)
                .on_conflict_do_nothing(index_elements=["metric", "source_ref"])
                .returning(Observation.id),
                rows,
            )
            .scalars()
            .all()
            if rows
            else []
        )
        conn.execute(
            update(SyncRun)
            .where(SyncRun.id == run_id)
            .values(
                status="ok",
                finished_at=clock.now(),
                rows_new=len(new_ids),
                complete_through={metric: day.isoformat() for metric, day in complete.items()},
                watch_upload_at=batch.watch_upload_at,
                watch_device=batch.watch_device,
            )
        )
        if batch.watch_waiting is not None:
            day, deadline = batch.watch_waiting
            enqueue(
                conn,
                clock,
                category=Category.ADMIN_ALERTS,
                payload={
                    "kind": "watch_sync_waiting",
                    "day": day.isoformat(),
                    "deadline": deadline.isoformat(),
                    "deadline_local": f"{deadline.astimezone(tz):%H:%M}",
                },
                dedupe_key=f"watch_sync_waiting:{day.isoformat()}",
            )
    log.info(
        "sync_ok",
        provider=provider.name,
        run_id=run_id,
        rows_new=len(new_ids),
        rows_corrected=corrected,
    )
    # A sync that heard from Garmin about the watch is healthy even with nothing new:
    # the watch just hasn't uploaded (a trip), and its days push at the deadline instead
    # of waiting on "no new data" failures (issue #57).
    stale = _stale_since(engine, clock.now()) if batch.watch_upload_at is None else None
    if stale is not None:
        hours = int((clock.now() - stale).total_seconds() // 3600)
        return _fail(engine, clock, provider.name, run_id, f"no new data for {hours} h")
    return SyncResult(run_id, "ok", len(new_ids), rows_corrected=corrected)


def _stale_since(engine: Engine, now: datetime) -> datetime | None:
    """During an active season, when new data last arrived if that was STALE_AFTER ago
    or more; None while data is fresh or no season is running."""
    with engine.connect() as conn:
        if active_season_id(conn) is None:
            return None
        last_new = conn.execute(
            select(func.max(SyncRun.finished_at)).where(SyncRun.rows_new > 0)
        ).scalar_one_or_none()
        first_run = conn.execute(select(func.min(SyncRun.started_at))).scalar_one_or_none()
    reference = last_new or first_run
    if reference is None:
        return None
    if reference.tzinfo is None:  # func.max() bypasses UTCDateTime
        reference = reference.replace(tzinfo=UTC)
    return reference if now - reference >= STALE_AFTER else None


# Built once: this runs on every sync. Newest last, so the dict below keeps the latest.
_STORED_TOTALS = (
    select(Observation.metric, Observation.local_date, Observation.value)
    .where(
        Observation.metric.in_(COUNT_METRICS),
        Observation.local_date.between(bindparam("first"), bindparam("last")),
    )
    .order_by(Observation.observed_at, Observation.id)
)


def _corrections(
    conn: Connection, rows: list[dict[str, Any]], run_id: int
) -> tuple[list[dict[str, Any]], int]:
    """Drop daily totals equal to the stored one; a changed one gets a ref naming this
    run, so it is stored as a new row next to the old (observations are append-only)."""
    totals = [r for r in rows if r["metric"] in COUNT_METRICS]
    if not totals:
        return rows, 0
    days = {
        "first": min(r["local_date"] for r in totals),
        "last": max(r["local_date"] for r in totals),
    }
    stored = {(m, day): value for m, day, value in conn.execute(_STORED_TOTALS, days)}
    kept: list[dict[str, Any]] = []
    corrected = 0
    for row in rows:
        before = (
            stored.get((row["metric"], row["local_date"]))
            if row["metric"] in COUNT_METRICS
            else None
        )
        if before is not None:
            if before == row["value"]:
                continue
            row = row | {"source_ref": f"{row['source_ref']}@{run_id}"}
            corrected += 1
        kept.append(row)
    return kept, corrected


def remind_watch_sync(engine: Engine, clock: Clock, day: date, cutoff: time) -> bool:
    """The bedtime reminder (issue #57): once per day at the cutoff, during a season.
    False when it was already queued or no season is running."""
    with immediate(engine) as conn:
        if active_season_id(conn) is None:
            return False
        return enqueue(
            conn,
            clock,
            category=Category.ADMIN_ALERTS,
            payload={
                "kind": "watch_sync_reminder",
                "day": day.isoformat(),
                "cutoff": f"{cutoff:%H:%M}",
            },
            dedupe_key=f"watch_sync_reminder:{day.isoformat()}",
        )


def record_failure(engine: Engine, clock: Clock, provider: str, error: str) -> SyncResult:
    """A sync that could not even start (e.g. no Garmin token yet) still shows up as a
    failed run and alerts the admin."""
    with immediate(engine) as conn:
        run_id = conn.execute(
            insert(SyncRun)
            .values(provider=provider, started_at=clock.now(), status="running", rows_new=0)
            .returning(SyncRun.id)
        ).scalar_one()
    return _fail(engine, clock, provider, run_id, error[:MAX_ERROR_LENGTH])


def _fail(engine: Engine, clock: Clock, provider: str, run_id: int, error: str) -> SyncResult:
    """Mark the run failed and queue one admin alert per failure streak (keyed on the
    last good run), so a broken sync alerts once, not every 15 minutes."""
    with immediate(engine) as conn:
        conn.execute(
            update(SyncRun)
            .where(SyncRun.id == run_id)
            .values(status="failed", finished_at=clock.now(), error=error)
        )
        last_ok = conn.execute(
            select(func.max(SyncRun.id)).where(SyncRun.status == "ok")
        ).scalar_one_or_none()
        enqueue(
            conn,
            clock,
            category=Category.ADMIN_ALERTS,
            payload={"kind": "sync_failed", "provider": provider, "error": error},
            dedupe_key=f"sync_failed:{last_ok or 0}",
        )
    log.error("sync_failed", provider=provider, run_id=run_id, error=error)
    return SyncResult(run_id, "failed", 0, error)
