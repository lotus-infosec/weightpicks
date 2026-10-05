"""Write outbox rows inside the caller's transaction (BUILD_PLAN §1.4.5).

`dedupe_key` is unique, so a retried business transaction can never queue the same
notification twice. Payloads carry ids and display values only: never emails,
webhook URLs or secrets. app/notify/dispatcher.py delivers them.
"""

from enum import StrEnum
from typing import Any

from sqlalchemy import Connection
from sqlalchemy.dialects.sqlite import insert

from app.core.clock import Clock
from app.models import OutboxMessage


class Category(StrEnum):
    BETS_PLACED = "bets_placed"
    HIGH_ROLLER = "high_roller"
    BET_RESULTS = "bet_results"
    MARKET_SETTLEMENTS = "market_settlements"
    ADMIN_ALERTS = "admin_alerts"
    BUSTS = "busts"
    NEW_MARKETS = "new_markets"
    WEEKLY_STANDINGS = "weekly_standings"
    PARLAY_RESULTS = "parlay_results"
    SPECIAL_EVENTS = "special_events"
    HYPE = "hype"
    GOAL_REACHED = "goal_reached"
    ACCOUNT_EMAIL = "account_email"  # channel "email": password resets, SMTP tests


def enqueue(
    conn: Connection,
    clock: Clock,
    *,
    category: Category,
    payload: dict[str, Any],
    dedupe_key: str,
    channel: str = "discord",
) -> bool:
    """Queue a message; False if one with this dedupe key already exists."""
    now = clock.now()
    result = conn.execute(
        insert(OutboxMessage)
        .values(
            channel=channel,
            category=category.value,
            payload=payload,
            dedupe_key=dedupe_key,
            status="pending",
            attempts=0,
            next_attempt_at=now,
            created_at=now,
        )
        .on_conflict_do_nothing(index_elements=["dedupe_key"])
    )
    return result.rowcount == 1
