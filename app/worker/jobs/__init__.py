from app.worker.jobs.demo import DemoDailyJob
from app.worker.jobs.heartbeat import HeartbeatJob
from app.worker.jobs.ledger_verify import LedgerVerifyJob
from app.worker.registry import Job

ALL_JOBS: tuple[Job, ...] = (HeartbeatJob(), DemoDailyJob(), LedgerVerifyJob())

__all__ = ["ALL_JOBS", "DemoDailyJob", "HeartbeatJob", "LedgerVerifyJob"]
