"""One AI call with its `ai_runs` bookkeeping.

The estimated neurons are reserved (status `running`) in a short transaction before the
request, so the cap holds even if the worker dies mid-call. The HTTP call itself runs
outside any transaction. Every outcome is recorded; nothing here raises to the caller.
"""

from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import Engine, insert, update

from app.ai import quota
from app.ai.client import AiError, AiSkip, Result, WorkersAI
from app.core.clock import Clock
from app.core.db import immediate
from app.models import AiRun

log = structlog.get_logger()
PROMPTS = Path(__file__).with_name("prompts")
RAW_LIMIT = 8192


@cache
def prompt(name: str) -> str:
    """A versioned prompt file, e.g. `props_v1`."""
    return (PROMPTS / f"{name}.txt").read_text(encoding="utf-8")


@dataclass(frozen=True, slots=True)
class CallOutcome:
    run_id: int
    result: Result | None
    status: str  # ok | skipped | error
    reason: str | None = None


def record_skip(engine: Engine, real_clock: Clock, kind: str, reason: str) -> int:
    with immediate(engine) as conn:
        run_id: int = conn.execute(
            insert(AiRun)
            .values(
                kind=kind,
                started_at=real_clock.now(),
                input_tokens=0,
                output_tokens=0,
                neurons_est=0,
                status="skipped",
                skip_reason=reason,
                errors=[],
            )
            .returning(AiRun.id)
        ).scalar_one()
    log.info("ai_run_skipped", kind=kind, reason=reason, ai_run_id=run_id)
    return run_id


def finish(engine: Engine, run_id: int, **values: Any) -> None:
    with immediate(engine) as conn:
        conn.execute(update(AiRun).where(AiRun.id == run_id).values(**values))


def call(
    engine: Engine,
    real_clock: Clock,
    client: WorkersAI | None,
    *,
    kind: str,
    model: str,
    prompt_version: str,
    messages: list[dict[str, str]],
    max_tokens: int,
    cap: int,
    json_schema: dict[str, Any] | None = None,
) -> CallOutcome:
    if client is None:
        skipped = record_skip(engine, real_clock, kind, "no_token")
        return CallOutcome(skipped, None, "skipped", "no_token")
    text = "".join(m["content"] for m in messages)
    est_in, est_neurons = quota.estimate(model, text, max_tokens)
    with immediate(engine) as conn:
        now = real_clock.now()
        allowed = quota.allows(conn, now, cap, est_neurons)
        run_id: int = conn.execute(
            insert(AiRun)
            .values(
                kind=kind,
                model=model,
                prompt_version=prompt_version,
                started_at=now,
                input_tokens=est_in if allowed else 0,
                output_tokens=max_tokens if allowed else 0,
                neurons_est=est_neurons if allowed else 0,
                status="running" if allowed else "skipped",
                skip_reason=None if allowed else "quota",
                errors=[],
            )
            .returning(AiRun.id)
        ).scalar_one()
    if not allowed:
        log.warning("ai_run_skipped", kind=kind, reason="quota", ai_run_id=run_id)
        return CallOutcome(run_id, None, "skipped", "quota")
    try:
        result = client.run(model, messages, max_tokens=max_tokens, json_schema=json_schema)
    except AiSkip as exc:
        finish(engine, run_id, status="skipped", skip_reason=exc.reason)
        log.info("ai_run_skipped", kind=kind, reason=exc.reason, ai_run_id=run_id)
        return CallOutcome(run_id, None, "skipped", exc.reason)
    except AiError as exc:
        raw = exc.raw[:RAW_LIMIT] if exc.raw else None
        finish(engine, run_id, status="error", skip_reason=exc.reason, raw_output=raw)
        log.warning("ai_run_failed", kind=kind, reason=exc.reason, ai_run_id=run_id)
        return CallOutcome(run_id, None, "error", exc.reason)
    used_in = result.input_tokens if result.input_tokens is not None else est_in
    used_out = result.output_tokens if result.output_tokens is not None else max_tokens
    finish(
        engine,
        run_id,
        status="ok",
        input_tokens=used_in,
        output_tokens=used_out,
        neurons_est=quota.neurons(model, used_in, used_out),
        raw_output=result.raw[:RAW_LIMIT],
    )
    log.info("ai_run_ok", kind=kind, ai_run_id=run_id, input_tokens=used_in, output_tokens=used_out)
    return CallOutcome(run_id, result, "ok")
