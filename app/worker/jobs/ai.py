"""AI jobs (BUILD_PLAN §1.4.4, §1.4.6; D-042): prop proposals after the daily and weekly
drops, and stat-update hype after the daily drop. Each runs once per period on the app
clock; the neuron quota uses real time. With the flag off they do nothing, and any AI
problem is recorded as an `ai_run`, never raised: core markets never wait on AI."""

from datetime import datetime

from app.core.clock import Clock, SystemClock
from app.core.config import Settings
from app.domain.schedule import at_local, latest_daily, latest_weekly, local_date
from app.services import ai_hype, ai_props
from app.services.instance import InstanceConfig
from app.worker.registry import JobContext


class AiPropsJob:
    """`props_daily` after each daily drop; `props_weekly` after the weekly drop."""

    def __init__(
        self,
        kind: str,
        settings: Settings,
        config: InstanceConfig,
        real_clock: Clock | None = None,
    ) -> None:
        self.kind = kind
        self.name = f"ai_{kind}"
        self.settings = settings
        self.config = config
        self.real_clock = real_clock or SystemClock()

    def due(self, now: datetime) -> str | None:
        if not self.config.flags.get("ai_props"):
            return None  # the worker rebuilds its jobs when a flag changes
        sched, tz = self.config.schedule, self.config.tz
        if self.kind == "props_weekly":
            day = latest_weekly(now, sched.weekly_drop_weekday, sched.weekly_drop, tz)
        else:
            day = latest_daily(now, sched.daily_drop, tz)
        return day.isoformat()

    def run(self, ctx: JobContext) -> int:
        # Catching up after the bet lock would only buy props that are already locked.
        today = local_date(ctx.now, self.config.tz)
        if ctx.period_key != today.isoformat() or ctx.now >= at_local(
            today, self.config.schedule.bet_lock, self.config.tz
        ):
            return 0
        report = ai_props.run(ctx.engine, ctx.clock, self.real_clock, self.settings, self.kind)
        return len(report.created) + len(report.queued)


class AiHypeJob:
    """Stat-update posts once a day after the daily drop (only while `ai_hype` is on)."""

    name = "ai_hype"

    def __init__(
        self, settings: Settings, config: InstanceConfig, real_clock: Clock | None = None
    ) -> None:
        self.settings = settings
        self.config = config
        self.real_clock = real_clock or SystemClock()

    def due(self, now: datetime) -> str | None:
        if not self.config.flags.get("ai_hype"):
            return None
        return latest_daily(now, self.config.schedule.daily_drop, self.config.tz).isoformat()

    def run(self, ctx: JobContext) -> int:
        if ctx.period_key != local_date(ctx.now, self.config.tz).isoformat():
            return 0  # catch-up: skip (BUILD_PLAN §1.4.6)
        return ai_hype.run(ctx.engine, ctx.clock, self.real_clock, self.settings)
