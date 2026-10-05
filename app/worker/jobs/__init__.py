from app.core.config import Settings
from app.domain.markets import Timeframe
from app.services.instance import InstanceConfig
from app.worker.jobs.ai import AiHypeJob, AiPropsJob
from app.worker.jobs.economy import DailyAllowanceJob, WeeklyStandingsJob
from app.worker.jobs.garmin_sync import GarminSyncJob, SyncSignal
from app.worker.jobs.heartbeat import HeartbeatJob
from app.worker.jobs.ledger_verify import LedgerVerifyJob
from app.worker.jobs.markets import DropJob, DueCache, LockMarketsJob, SettleMarketsJob
from app.worker.jobs.pools import PoolsJob
from app.worker.jobs.season import GoalWatchJob
from app.worker.registry import Job

# Infrastructure jobs always run on real time (the container health check compares
# heartbeats with the wall clock). Domain jobs run on the app clock (SimClock in dev).
INFRA_JOBS: tuple[Job, ...] = (HeartbeatJob(),)


def domain_jobs(settings: Settings, config: InstanceConfig) -> tuple[Job, ...]:
    """In tick order: sync first so the 11:00 drop prices from the post-window sync,
    then drops, the lock pass and the settle pass."""
    locks, settles = DueCache(), DueCache()
    synced = SyncSignal()
    return (
        GarminSyncJob(settings, config, synced),
        GoalWatchJob(synced),
        DropJob(Timeframe.DAILY, config, locks),
        DropJob(Timeframe.WEEKLY, config, locks),
        DropJob(Timeframe.MONTHLY, config, locks),
        AiPropsJob("props_daily", settings, config),
        AiPropsJob("props_weekly", settings, config),
        AiHypeJob(settings, config),
        LockMarketsJob(locks, settles),
        SettleMarketsJob(settles),
        PoolsJob(),
        DailyAllowanceJob(config),
        WeeklyStandingsJob(config),
        LedgerVerifyJob(),
    )


__all__ = [
    "INFRA_JOBS",
    "AiHypeJob",
    "AiPropsJob",
    "DropJob",
    "GarminSyncJob",
    "GoalWatchJob",
    "HeartbeatJob",
    "LedgerVerifyJob",
    "LockMarketsJob",
    "PoolsJob",
    "SettleMarketsJob",
    "domain_jobs",
]
