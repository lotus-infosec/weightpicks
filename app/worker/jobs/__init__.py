from app.core.config import Settings
from app.worker.jobs.demo import DemoDailyJob
from app.worker.jobs.garmin_sync import GarminSyncJob
from app.worker.jobs.heartbeat import HeartbeatJob
from app.worker.jobs.ledger_verify import LedgerVerifyJob
from app.worker.registry import Job

# Infrastructure jobs always run on real time (the container health check compares
# heartbeats with the wall clock). Domain jobs run on the app clock (SimClock in dev).
INFRA_JOBS: tuple[Job, ...] = (HeartbeatJob(),)


def domain_jobs(settings: Settings) -> tuple[Job, ...]:
    return (GarminSyncJob(settings), DemoDailyJob(), LedgerVerifyJob())


__all__ = [
    "INFRA_JOBS",
    "DemoDailyJob",
    "GarminSyncJob",
    "HeartbeatJob",
    "LedgerVerifyJob",
    "domain_jobs",
]
