"""Web -> worker requests (the `commands` table), e.g. the admin's "Sync now".

Runs with the infrastructure jobs on every tick (real time), so it doesn't slow the
simulation fast-forward. A command is claimed by flipping pending -> running in one
transaction, so it can only run once.
"""

from datetime import datetime
from typing import Any

import structlog
from sqlalchemy import select, update

from app.core.clock import Clock
from app.core.config import Settings
from app.core.db import immediate
from app.models import Command
from app.services import ai_props, instance
from app.services.sync import make_provider, run_sync
from app.worker.registry import JobContext

log = structlog.get_logger()


class CommandsJob:
    name = "commands"
    every_tick = True

    def __init__(self, settings: Settings, domain_clock: Clock) -> None:
        self.settings = settings
        self.domain_clock = domain_clock  # a sync must use the app clock (SimClock in dev)

    def due(self, now: datetime) -> str:
        return "tick"

    def run(self, ctx: JobContext) -> int:
        with ctx.engine.connect() as conn:
            pending = conn.execute(
                select(Command.id, Command.type)
                .where(Command.status == "pending")
                .order_by(Command.id)
                .limit(1)
            ).one_or_none()
        if pending is None:
            return 0
        with immediate(ctx.engine) as conn:
            claimed = conn.execute(
                update(Command)
                .where(Command.id == pending.id, Command.status == "pending")
                .values(status="running")
            ).rowcount
        if not claimed:
            return 0
        status, result = "done", {}
        try:
            result = self.handle(ctx, pending.type)
        except Exception as exc:
            log.exception("command_failed", command_id=pending.id, type=pending.type)
            status, result = "failed", {"error": type(exc).__name__}
        with immediate(ctx.engine) as conn:
            conn.execute(
                update(Command)
                .where(Command.id == pending.id)
                .values(status=status, result=result, done_at=ctx.clock.now())
            )
        log.info("command_done", command_id=pending.id, type=pending.type, status=status)
        return 1

    def handle(self, ctx: JobContext, kind: str) -> dict[str, Any]:
        if kind == "sync_now":
            config = instance.load(ctx.engine, self.domain_clock, self.settings)
            provider = make_provider(self.settings, ctx.engine, self.domain_clock, tz=config.tz)
            sync = run_sync(ctx.engine, self.domain_clock, provider, tz=config.tz, unit=config.unit)
            return {"sync_status": sync.status, "rows_new": sync.rows_new}
        if kind == "ai_props_now":
            report = ai_props.run(
                ctx.engine, self.domain_clock, ctx.clock, self.settings, "props_manual"
            )
            return {
                "ai_status": report.status,
                "ai_reason": report.reason,
                "created": len(report.created),
                "queued": len(report.queued),
                "dropped": len(report.dropped),
            }
        raise ValueError(f"unknown command {kind!r}")
