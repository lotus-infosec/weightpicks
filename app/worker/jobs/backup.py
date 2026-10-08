"""Nightly automatic backup: 03:30 local, keep the newest N
(`BACKUP_RETENTION`, default 7). Runs on real time with the infrastructure jobs, so a
fast-forwarded SimClock never floods the disk. Catch-up after downtime: run once now."""

from datetime import datetime, time
from zoneinfo import ZoneInfo

from app.core.config import Settings
from app.domain.schedule import latest_daily
from app.services import backups
from app.worker.registry import JobContext

BACKUP_AT = time(3, 30)


class AutoBackupJob:
    name = "auto_backup"

    def __init__(self, settings: Settings, tz: ZoneInfo) -> None:
        self.settings = settings
        self.tz = tz

    def due(self, now: datetime) -> str:
        return latest_daily(now, BACKUP_AT, self.tz).isoformat()

    def run(self, ctx: JobContext) -> int:
        backups.create(ctx.engine, self.settings, ctx.clock, kind="auto")
        return len(backups.prune(self.settings, self.settings.backup_retention))
