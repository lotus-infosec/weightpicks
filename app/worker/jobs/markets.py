"""Market jobs: the daily/weekly/monthly drops and the every-tick lock pass (D-009)."""

from datetime import date, datetime, timedelta

from app.domain.markets import Timeframe
from app.domain.schedule import latest_daily, latest_month_end, latest_weekly
from app.services import markets, settlement
from app.services.instance import InstanceConfig
from app.worker.registry import JobContext

# Every-tick passes re-read their next due time at least this often (app time), so work
# created by another process is still picked up. Freezing locks in its own transaction,
# bet placement checks `lock_at` itself, and settlement already waits for 2-hourly syncs,
# so this lag only delays a status flip, never lets a late bet in (D-030, D-033).
RECHECK = timedelta(minutes=30)


class DueCache:
    """In-process cache of the next time an every-tick pass has work (earliest open
    lock_at, earliest locked settle_after). Whoever creates that work invalidates it."""

    def __init__(self) -> None:
        self.next_due: datetime | None = None
        self.checked_at: datetime | None = None

    def invalidate(self) -> None:
        self.checked_at = None

    def needs_check(self, now: datetime) -> bool:
        if self.checked_at is None or not self.checked_at <= now < self.checked_at + RECHECK:
            return True
        return self.next_due is not None and now >= self.next_due


class DropJob:
    """Posts one timeframe's core markets. Catch-up after downtime: latest period only."""

    def __init__(self, timeframe: Timeframe, config: InstanceConfig, locks: DueCache) -> None:
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

    def __init__(self, locks: DueCache, settles: DueCache) -> None:
        self.locks = locks
        self.settles = settles

    def due(self, now: datetime) -> str:
        return "tick"

    def run(self, ctx: JobContext) -> int:
        if not self.locks.needs_check(ctx.now):
            return 0
        result = markets.lock_due(ctx.engine, ctx.now)
        self.locks.next_due, self.locks.checked_at = result.next_lock_at, ctx.now
        if result.locked:
            self.settles.invalidate()
        return result.locked


class SettleMarketsJob:
    """Every tick: settle locked markets that pass the readiness gate; alert on stale."""

    name = "settle_markets"
    every_tick = True

    def __init__(self, settles: DueCache) -> None:
        self.settles = settles

    def due(self, now: datetime) -> str:
        return "tick"

    def run(self, ctx: JobContext) -> int:
        if not self.settles.needs_check(ctx.now):
            return 0
        result = settlement.settle_due(ctx.engine, ctx.clock)
        self.settles.next_due, self.settles.checked_at = result.next_settle_after, ctx.now
        return result.settled
