"""Daily neuron budget (BUILD_PLAN §2.4, D-014).

Each call is pre-estimated (characters ÷ 4 for input, `max_tokens` for output) and refused
if it would cross `AI_DAILY_NEURON_CAP`. Usage is the sum of `ai_runs.neurons_est` since
00:00 UTC on the real clock, matching Cloudflare's own daily reset.
"""

import math
from datetime import UTC, datetime

from sqlalchemy import Connection, func, select

from app.models import AiRun

# Neurons per million tokens (input, output), from the Workers AI pricing page.
RATES: dict[str, tuple[int, int]] = {
    "@cf/meta/llama-3.1-8b-instruct": (25_608, 75_147),
    "@cf/meta/llama-3.1-8b-instruct-fp8-fast": (4_119, 34_868),
}
DEFAULT_RATE = RATES["@cf/meta/llama-3.1-8b-instruct"]  # unknown model: assume the dearer


def tokens(text: str) -> int:
    return math.ceil(len(text) / 4)


def neurons(model: str, input_tokens: int, output_tokens: int) -> int:
    rate_in, rate_out = RATES.get(model, DEFAULT_RATE)
    return math.ceil((input_tokens * rate_in + output_tokens * rate_out) / 1_000_000)


def estimate(model: str, prompt: str, max_tokens: int) -> tuple[int, int]:
    """(input tokens, neurons) for a call before it is made."""
    input_tokens = tokens(prompt)
    return input_tokens, neurons(model, input_tokens, max_tokens)


def day_start(now: datetime) -> datetime:
    utc = now.astimezone(UTC)
    return utc.replace(hour=0, minute=0, second=0, microsecond=0)


def used_today(conn: Connection, now: datetime) -> int:
    total = conn.execute(
        select(func.coalesce(func.sum(AiRun.neurons_est), 0)).where(
            AiRun.started_at >= day_start(now)
        )
    ).scalar_one()
    return int(total)


def allows(conn: Connection, now: datetime, cap: int, cost: int) -> bool:
    return used_today(conn, now) + cost <= cap
