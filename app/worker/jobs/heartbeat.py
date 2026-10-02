from datetime import datetime

import structlog
from sqlalchemy.dialects.sqlite import insert

from app.core.db import immediate
from app.models import Heartbeat
from app.worker.registry import JobContext

log = structlog.get_logger()
COMPONENT = "worker"


class HeartbeatJob:
    """Proves the worker is alive; `wp health --worker` checks its age."""

    name = "heartbeat"

    def due(self, now: datetime) -> str:
        return now.strftime("%Y-%m-%dT%H:%M")

    def run(self, ctx: JobContext) -> None:
        stmt = insert(Heartbeat).values(component=COMPONENT, beat_at=ctx.now)
        with immediate(ctx.engine) as conn:
            conn.execute(
                stmt.on_conflict_do_update(
                    index_elements=[Heartbeat.component], set_={"beat_at": stmt.excluded.beat_at}
                )
            )
        log.info("heartbeat", component=COMPONENT)
