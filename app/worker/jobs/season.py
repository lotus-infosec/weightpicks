"""Goal watch (D-011): after each new successful sync, check whether a complete day's
canonical weigh-in reached the season goal; if so run Goal Reached once (its job key
`goal_reached` / `<season_id>` makes reruns and the manual trigger no-ops)."""

from datetime import date, datetime, timedelta

from app.services import season
from app.worker.jobs.garmin_sync import SyncSignal
from app.worker.registry import JobContext

# Scheduled syncs bump the signal; a sync started elsewhere (the admin's "Sync now" runs
# in the infra loop) is still noticed within this long.
FALLBACK = timedelta(hours=2)


class GoalWatchJob:
    name = "goal_watch"
    every_tick = True

    def __init__(self, signal: SyncSignal | None = None) -> None:
        self.signal = signal or SyncSignal()
        self.seen = -1
        self.checked_at: datetime | None = None
        self.scanned: tuple[int | None, date | None] = (None, None)  # (season, last day)
        # No active season with a goal: stand down. The goal only changes in /setup or a
        # new season, and both change the settings row, which rebuilds the worker's jobs.
        self.idle = False

    def due(self, now: datetime) -> str:
        return "tick"

    def run(self, ctx: JobContext) -> int:
        if self.idle:
            return 0
        fallback = self.checked_at is None or not (
            self.checked_at <= ctx.now < self.checked_at + FALLBACK
        )
        if self.signal.version == self.seen and not fallback:
            return 0
        self.seen, self.checked_at = self.signal.version, ctx.now
        with ctx.engine.connect() as conn:
            result = season.scan(conn, after=self.scanned)
        self.scanned = (result.season_id, result.complete)
        self.idle = not result.watching
        if result.hit is None:
            return 0
        return 1 if season.goal_reached(ctx.engine, ctx.clock, hit=result.hit) else 0
