"""Every-tick pool pass (D-043): lock pools at their lock time, settle them once the
target day's weigh-in is complete. Cheap between due times thanks to the DueCache."""

from datetime import datetime, timedelta

from app.services import pools
from app.worker.jobs.markets import DueCache
from app.worker.registry import JobContext

# Pools are created in the web process, and the earliest lock is the night before a target
# date at least two days out, so a two-hour re-read is plenty; entry checks `lock_at`
# itself, so this lag only delays the status flip.
RECHECK = timedelta(hours=2)


class PoolsJob:
    name = "pools_tick"
    every_tick = True

    def __init__(self, due: DueCache | None = None) -> None:
        self.cache = due or DueCache(RECHECK)

    def due(self, now: datetime) -> str:
        return "tick"

    def run(self, ctx: JobContext) -> int:
        if not self.cache.needs_check(ctx.now):
            return 0
        result = pools.tick(ctx.engine, ctx.clock)
        self.cache.next_due, self.cache.checked_at = result.next_due, ctx.now
        return result.locked + result.finished
