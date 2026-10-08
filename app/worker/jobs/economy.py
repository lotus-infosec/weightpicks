"""Scheduled economy and social jobs on the app clock."""

from datetime import date, datetime, time, timedelta

from app.domain.schedule import at_local, latest_daily, latest_weekly
from app.services import allowance, standings
from app.services.instance import InstanceConfig
from app.worker.registry import JobContext

ALLOWANCE_AT = time(0, 5)
STANDINGS_WEEKDAY, STANDINGS_AT = 0, time(9, 0)  # Monday 09:00
STANDINGS_LATE = timedelta(hours=24)  # after a long outage, skip rather than post days late


class DailyAllowanceJob:
    """00:05 every day; one run pays every missed day too (catch-up in the service)."""

    name = "daily_allowance"

    def __init__(self, config: InstanceConfig) -> None:
        self.config = config

    def due(self, now: datetime) -> str:
        return latest_daily(now, ALLOWANCE_AT, self.config.tz).isoformat()

    def run(self, ctx: JobContext) -> None:
        allowance.pay_due(ctx.engine, ctx.clock, self.config.tz, date.fromisoformat(ctx.period_key))


class WeeklyStandingsJob:
    """Monday 09:00 local; latest week only."""

    name = "weekly_standings"

    def __init__(self, config: InstanceConfig) -> None:
        self.config = config

    def due(self, now: datetime) -> str:
        tz = self.config.tz
        return latest_weekly(now, STANDINGS_WEEKDAY, STANDINGS_AT, tz).isoformat()

    def run(self, ctx: JobContext) -> None:
        monday = date.fromisoformat(ctx.period_key)
        if ctx.now - at_local(monday, STANDINGS_AT, self.config.tz) > STANDINGS_LATE:
            return
        standings.post_week(ctx.engine, ctx.clock, self.config.tz, monday)
