from datetime import datetime

from app.core.config import Settings
from app.domain.schedule import sync_period_key
from app.providers.base import DataProvider
from app.services.sync import make_provider, run_sync
from app.worker.registry import JobContext


class SyncFailed(Exception):
    pass


class GarminSyncJob:
    """Sync + ingest: every 15 min in the weigh-in window, every 2 h otherwise."""

    name = "garmin_sync"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._provider: DataProvider | None = None

    def due(self, now: datetime) -> str:
        return sync_period_key(now, self.settings.tz)

    def run(self, ctx: JobContext) -> None:
        provider = make_provider(self.settings, ctx.engine, ctx.clock, self._provider)
        self._provider = provider
        result = run_sync(
            ctx.engine, ctx.clock, provider, tz=self.settings.tz, unit=self.settings.wp_unit
        )
        if result.status != "ok":
            raise SyncFailed(result.error)
