from app.core.config import Settings
from app.domain.markets import Timeframe
from app.services.instance import InstanceConfig
from app.worker.jobs.garmin_sync import GarminSyncJob
from app.worker.jobs.heartbeat import HeartbeatJob
from app.worker.jobs.ledger_verify import LedgerVerifyJob
from app.worker.jobs.markets import DropJob, DueCache, LockMarketsJob, SettleMarketsJob
from app.worker.registry import Job

# Infrastructure jobs always run on real time (the container health check compares
# heartbeats with the wall clock). Domain jobs run on the app clock (SimClock in dev).
INFRA_JOBS: tuple[Job, ...] = (HeartbeatJob(),)


def domain_jobs(settings: Settings, config: InstanceConfig) -> tuple[Job, ...]:
    """In tick order: sync first so the 11:00 drop prices from the post-window sync,
    then drops, the lock pass and the settle pass."""
    locks, settles = DueCache(), DueCache()
    return (
        GarminSyncJob(settings, config),
        DropJob(Timeframe.DAILY, config, locks),
        DropJob(Timeframe.WEEKLY, config, locks),
        DropJob(Timeframe.MONTHLY, config, locks),
        LockMarketsJob(locks, settles),
        SettleMarketsJob(settles),
        LedgerVerifyJob(),
    )


__all__ = [
    "INFRA_JOBS",
    "DropJob",
    "GarminSyncJob",
    "HeartbeatJob",
    "LedgerVerifyJob",
    "LockMarketsJob",
    "SettleMarketsJob",
    "domain_jobs",
]
