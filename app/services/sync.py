"""Provider sync and ingest (BUILD_PLAN §1.4.6, §2.3).

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
from app.providers.simulated import SimulatedProvider
from app.services import sim

log = structlog.get_logger()
FETCH_OVERLAP = timedelta(days=1)
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


def make_provider(settings: Settings, engine: Engine, clock: Clock) -> DataProvider:
    if settings.data_provider == "simulated":
        with immediate(engine) as conn:
            state = sim.ensure_state(
                conn, clock, seed=settings.sim_seed, tz_name=settings.wp_timezone
            )
        return SimulatedProvider(
            preset=state.preset, seed=state.seed, anchor_date=state.anchor_date, tz=settings.tz
        )
    raise ProviderUnavailable("the GarminDB provider arrives in STAGE10")


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
        error = repr(exc)[:MAX_ERROR_LENGTH]
        with immediate(engine) as conn:
            conn.execute(
                update(SyncRun)
                .where(SyncRun.id == run_id)
                .values(status="failed", finished_at=clock.now(), error=error)
            )
        log.error("sync_failed", provider=provider.name, run_id=run_id, error=error)
        return SyncResult(run_id, "failed", 0, error)

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
    return SyncResult(run_id, "ok", len(new_ids))
