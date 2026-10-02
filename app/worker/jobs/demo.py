from datetime import datetime

import structlog

from app.worker.registry import JobContext

log = structlog.get_logger()


class DemoDailyJob:
    """Placeholder daily job proving once-per-day keys. Uses the UTC date until the
    instance time zone exists (set in /setup, STAGE08)."""

    name = "demo_daily"

    def due(self, now: datetime) -> str:
        return now.date().isoformat()

    def run(self, ctx: JobContext) -> None:
        log.info("demo_daily", period_key=ctx.period_key)
