"""Market jobs: the daily/weekly/monthly drops and the every-tick lock pass (D-009)."""

from datetime import date, datetime, timedelta

from app.domain.markets import Timeframe
from app.domain.schedule import latest_daily, latest_month_end, latest_weekly
from app.services import markets
from app.services.instance import InstanceConfig
from app.worker.registry import JobContext

# The lock pass re-reads the next lock time at least this often (app time), so markets
# created or a freeze made by another process are still picked up. Freezing locks in its
# own transaction, and bet placement checks `lock_at` itself (STAGE06), so this lag only
# delays the status flip, never lets a late bet in.
LOCK_RECHECK = timedelta(minutes=15)


class LockSchedule:
    """In-process cache of the earliest open market's lock time. Drops invalidate it."""

    def __init__(self) -> None:
        self.next_lock: datetime | None = None
        self.checked_at: datetime | None = None

    def invalidate(self) -> None:
        self.checked_at = None

    def needs_check(self, now: datetime) -> bool:
        if self.checked_at is None or not self.checked_at <= now < self.checked_at + LOCK_RECHECK:
            return True
        return self.next_lock is not None and now >= self.next_lock


class DropJob:
    """Posts one timeframe's core markets. Catch-up after downtime: latest period only."""

    def __init__(self, timeframe: Timeframe, config: InstanceConfig, locks: LockSchedule) -> None:
        self.timeframe = timeframe
        self.name = f"{timeframe.value}_drop"
        self.config = config
        self.locks = locks

    def due(self, now: datetime) -> str:
        sched, tz = self.config.schedule, self.config.tz
        if self.timeframe is Timeframe.DAILY:
            day = latest_daily(now, sched.daily_drop, tz)
        elif self.timeframe is Timeframe.WEEKLY:
            day = latest_weekly(now, sched.weekly_drop_weekday, sched.weekly_drop, tz)
        else:
            day = latest_month_end(now, sched.monthly_drop, tz)
        return day.isoformat()

    def run(self, ctx: JobContext) -> None:
        day = date.fromisoformat(ctx.period_key)
        markets.drop(ctx.engine, ctx.clock, self.config, self.timeframe, day)
        self.locks.invalidate()


class LockMarketsJob:
    """Every tick: lock open markets whose lock time has passed (all of them if frozen)."""

    name = "lock_markets"
    every_tick = True

    def __init__(self, locks: LockSchedule) -> None:
        self.locks = locks

    def due(self, now: datetime) -> str:
        return "tick"

    def run(self, ctx: JobContext) -> int:
        if not self.locks.needs_check(ctx.now):
            return 0
        result = markets.lock_due(ctx.engine, ctx.now)
        self.locks.next_lock, self.locks.checked_at = result.next_lock_at, ctx.now
        return result.locked
