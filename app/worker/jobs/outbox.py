"""Infra job: drain the outbox to Discord on real time."""

from datetime import datetime

from app.notify.dispatcher import Dispatcher
from app.worker.registry import JobContext


class OutboxDispatchJob:
    name = "outbox_dispatch"
    every_tick = True

    def __init__(self, dispatcher: Dispatcher) -> None:
        self.dispatcher = dispatcher
        self.more = False  # the worker loop waits 2 s instead of a full tick while True

    def due(self, now: datetime) -> str:
        return "tick"

    def run(self, ctx: JobContext) -> int:
        result = self.dispatcher.run_pass(ctx.engine)
        self.more = result.more
        return result.sent
