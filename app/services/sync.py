"""Provider sync and ingest.

The provider is called outside any database transaction (the real GarminDB sync is a
slow subprocess); only the ingest itself is one short BEGIN IMMEDIATE write.
Observations are inserted with ON CONFLICT DO NOTHING on (metric, source_ref), so
overlapping fetches and replays never duplicate data.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import Engine, func, select, update
from sqlalchemy.dialects.sqlite import insert

from app.core.clock import Clock
from app.core.config import Settings
from app.core.db import immediate
from app.domain.schedule import in_weigh_in_window, local_date, next_local_midnight
from app.domain.units import Unit, grams_to_tenths
from app.models import Observation, SyncRun
from app.providers.base import Batch, DataProvider
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


def make_provider(
    settings: Settings,
    engine: Engine,
    clock: Clock,
    cached: DataProvider | None = None,
    *,
    tz: ZoneInfo | None = None,
) -> DataProvider:
    """The configured provider. A cached simulator is reused while its config is unchanged
    (keeping its per-day data cache); a reseed produces a fresh one."""
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
    if isinstance(cached, GarminDBProvider) and cached.tz == zone:
        return cached
    if not (settings.garmin_home / ".GarminDb" / "garmin_tokens.json").is_file():
        raise ProviderUnavailable("no Garmin token yet: run garmin-login once")
    runner = GarminDBRunner(settings.garmin_home, settings.garmindb_python, zone.key)
    return GarminDBProvider(settings.garmin_home, zone, runner)


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
    engine: Engine, clock: Clock, provider: DataProvider, *, tz: ZoneInfo, unit: Unit
) -> SyncResult:
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

    try:
        batch = provider.fetch_since(since, now)
        complete = provider.complete_through(now)
    except Exception as exc:
        # GarminRunError messages are already scrubbed; anything else is shown by type.
        error = (str(exc) if isinstance(exc, GarminRunError) else repr(exc))[:MAX_ERROR_LENGTH]
        return _fail(engine, clock, provider.name, run_id, error)

    rows = _rows(batch, provider.name, run_id, tz, unit)
    with immediate(engine) as conn:
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
            )
        )
    log.info("sync_ok", provider=provider.name, run_id=run_id, rows_new=len(new_ids))
    stale = _stale_since(engine, clock.now())
    if stale is not None:
        hours = int((clock.now() - stale).total_seconds() // 3600)
        return _fail(engine, clock, provider.name, run_id, f"no new data for {hours} h")
    return SyncResult(run_id, "ok", len(new_ids))


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
