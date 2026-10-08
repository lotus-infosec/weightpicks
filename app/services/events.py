"""Special-event builder: the admin's plain text -> a Workers AI
draft `{title, question, target_date, buy_in}` -> the same strict validation as the manual
form (`pools.validate`) -> preview -> publish. AI never picks winners; code settles."""

import json
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import httpx
from sqlalchemy import Connection, Engine, select

from app.ai import runs
from app.ai.client import from_store
from app.core.clock import Clock
from app.core.config import Settings
from app.domain.lines import fit_weight
from app.domain.schedule import local_date
from app.models import Season
from app.services import instance, pools
from app.services.ai_props import bettor_names
from app.services.instance import InstanceConfig
from app.services.ledger import active_season_id
from app.services.observations import canonical_weigh_ins

PROMPT_VERSION = "event_v1"
MAX_TOKENS = 250
TEXT_MAX = 500
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "maxLength": 80},
        "question": {"type": "string", "maxLength": 300},
        "target_date": {"type": "string"},
        "buy_in_dollars": {"type": "number"},
    },
    "required": ["title", "question", "target_date", "buy_in_dollars"],
}


def projection(conn: Connection, config: InstanceConfig, today: date, target: date) -> float | None:
    """The engine's trend projection for `target`, shown in the preview for context."""
    history = canonical_weigh_ins(conn, today - timedelta(days=83), today)
    points = [((c.local_date - today).days, c.value / 10) for c in history]
    if not any(t > -14 for t, _ in points):
        return None
    fit = fit_weight(points, config.unit)
    return round(fit.a + fit.b * (target - today).days, 1)


def context(conn: Connection, config: InstanceConfig, today: date, text: str) -> dict[str, Any]:
    history = canonical_weigh_ins(conn, today - timedelta(days=13), today)
    season_id = active_season_id(conn)
    goal = (
        conn.execute(select(Season.goal_weight_x10).where(Season.id == season_id)).scalar_one()
        if season_id
        else None
    )
    economy = config.economy
    return {
        "today": today.isoformat(),
        "weekday": today.strftime("%A"),
        "unit": config.unit,
        "latest_weight": history[-1].value / 10 if history else None,
        "trend_per_week": (
            round(
                (history[-1].value - history[0].value)
                / 10
                / max(1, (history[-1].local_date - history[0].local_date).days)
                * 7,
                2,
            )
            if len(history) >= 2
            else None
        ),
        "season_goal": goal / 10 if goal else None,
        "target_date_range": {
            "min": (today + timedelta(days=pools.MIN_DAYS)).isoformat(),
            "max": (today + timedelta(days=pools.MAX_DAYS)).isoformat(),
        },
        "buy_in_dollars": {
            "default": economy.pool_buyin_cents / 100,
            "min": 1,
            "max": economy.max_pool_buyin_cents / 100,
        },
        "admin_text": text,
    }


@dataclass(frozen=True, slots=True)
class AiDraft:
    draft: pools.Draft | None
    run_id: int | None
    error: str | None = None
    raw: dict[str, Any] | None = None  # the AI fields, to refill the form on a refusal


def draft_with_ai(
    engine: Engine,
    clock: Clock,
    real_clock: Clock,
    settings: Settings,
    text: str,
    *,
    transport: httpx.BaseTransport | None = None,
) -> AiDraft:
    """Ask Workers AI for a draft and validate it. Never raises for AI problems."""
    text = " ".join(text.split())[:TEXT_MAX]
    if not text:
        return AiDraft(None, None, "Describe the event first.")
    with engine.connect() as conn:
        config = instance.read(conn)
        if config is None:
            return AiDraft(None, None, "Finish setup first.")
        today = local_date(clock.now(), config.tz)
        payload = context(conn, config, today, text)
        client = from_store(conn, settings, transport)
        names = bettor_names(conn)
    outcome = runs.call(
        engine,
        real_clock,
        client,
        kind="event_builder",
        model=settings.ai_model_json,
        prompt_version=PROMPT_VERSION,
        messages=[
            {"role": "system", "content": runs.prompt(PROMPT_VERSION)},
            {"role": "user", "content": json.dumps(payload, separators=(",", ":"))},
        ],
        max_tokens=MAX_TOKENS,
        cap=settings.ai_daily_neuron_cap,
        json_schema=SCHEMA,
    )
    if outcome.result is None:
        why = {
            "no_token": "Workers AI isn't set up. Fill in the form by hand.",
            "quota": "Today's AI budget is used up. Fill in the form by hand.",
        }.get(outcome.reason or "", "The AI draft didn't work this time. Fill in the form by hand.")
        return AiDraft(None, outcome.run_id, why)
    data = outcome.result.data if isinstance(outcome.result.data, dict) else {}
    try:
        cents = round(float(data.get("buy_in_dollars")) * 100)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        cents = None
    raw = {
        "title": data.get("title"),
        "question": data.get("question"),
        "target_date": data.get("target_date"),
        "buy_in_cents": cents,
    }
    try:
        draft = pools.validate(config, clock.now(), raw, names=names)
    except pools.PoolError as exc:
        runs.finish(engine, outcome.run_id, errors=[{"reason": exc.code}])
        return AiDraft(None, outcome.run_id, f"The AI draft was refused: {exc.message}", raw)
    return AiDraft(draft, outcome.run_id, None, raw)
