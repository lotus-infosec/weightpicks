"""Every-tick pool pass (D-043): lock pools at their lock time, settle them once the
target day's weigh-in is complete. Cheap between due times thanks to the DueCache."""

from datetime import datetime

from app.services import pools
from app.worker.jobs.markets import DueCache
from app.worker.registry import JobContext


class PoolsJob:
    name = "pools_tick"
    every_tick = True

    def __init__(self, due: DueCache | None = None) -> None:
        self.cache = due or DueCache()

    def due(self, now: datetime) -> str:
        return "tick"

    def run(self, ctx: JobContext) -> int:
        if not self.cache.needs_check(ctx.now):
            return 0
        result = pools.tick(ctx.engine, ctx.clock)
        self.cache.next_due, self.cache.checked_at = result.next_due, ctx.now
        return result.locked + result.finished
