from datetime import datetime

from app.core.config import Settings
from app.domain.schedule import sync_period_key
from app.providers.base import DataProvider
from app.services.instance import InstanceConfig
from app.services.sync import ProviderUnavailable, make_provider, record_failure, run_sync
from app.worker.registry import JobContext


class SyncFailed(Exception):
    pass


class SyncSignal:
    """Bumped after each successful scheduled sync, so jobs that only care about new data
    (the goal watch) can skip the database until there is some."""

    def __init__(self) -> None:
        self.version = 0

    def bump(self) -> None:
        self.version += 1


class GarminSyncJob:
    """Sync + ingest: every 15 min in the weigh-in window, every 2 h otherwise."""

    name = "garmin_sync"

    def __init__(
        self, settings: Settings, config: InstanceConfig, signal: SyncSignal | None = None
    ) -> None:
        self.settings = settings
        self.config = config  # zone and unit come from the settings row (D-036)
        self.signal = signal or SyncSignal()
        self._provider: DataProvider | None = None

    def due(self, now: datetime) -> str:
        return sync_period_key(now, self.config.tz)

    def run(self, ctx: JobContext) -> None:
        try:
            provider = make_provider(
                self.settings, ctx.engine, ctx.clock, self._provider, tz=self.config.tz
            )
        except ProviderUnavailable as exc:
            record_failure(ctx.engine, ctx.clock, self.settings.data_provider, str(exc))
            raise SyncFailed(str(exc)) from exc
        self._provider = provider
        result = run_sync(ctx.engine, ctx.clock, provider, tz=self.config.tz, unit=self.config.unit)
        if result.status != "ok":
            raise SyncFailed(result.error)
        self.signal.bump()
