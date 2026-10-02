"""Worker process: wait for the migrated schema, then tick every 60 s until SIGTERM."""

import signal
import threading
from types import FrameType

import structlog

from app.core.clock import SystemClock
from app.core.config import Settings
from app.core.db import make_engine
from app.core.logging import configure_logging
from app.core.migrations import is_at_head
from app.worker.jobs import ALL_JOBS
from app.worker.registry import run_due

TICK_SECONDS = 60
SCHEMA_POLL_SECONDS = 2

log = structlog.get_logger()


def main() -> None:
    settings = Settings()
    configure_logging(settings.log_level, settings.log_format)
    engine = make_engine(settings.db_url)
    clock = SystemClock()
    stop = threading.Event()

    def _stop(signum: int, _frame: FrameType | None) -> None:
        log.info("worker_stopping", signal=signal.Signals(signum).name)
        stop.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    while not stop.is_set() and not is_at_head(engine):
        log.info("waiting_for_schema")
        stop.wait(SCHEMA_POLL_SECONDS)

    log.info("worker_started", tick_seconds=TICK_SECONDS, jobs=[j.name for j in ALL_JOBS])
    while not stop.is_set():
        try:
            run_due(ALL_JOBS, engine, clock)
        except Exception:
            log.exception("tick_failed")
        stop.wait(TICK_SECONDS)
    engine.dispose()
    log.info("worker_stopped")


if __name__ == "__main__":
    main()
