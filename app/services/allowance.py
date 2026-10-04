"""Daily allowance (BUILD_PLAN §1.4.6, D-040): every non-banned player (active, frozen or
bust) gets `economy.daily_allowance_cents` once per local day, from the day after they
joined the season. Missed days are caught up (up to 31). Allowances move balance only,
never P&L (D-007), and can lift a bust back over $1."""

from datetime import date, timedelta
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import Engine, select

from app.core.clock import Clock
from app.core.db import immediate
from app.domain.ledger import AccountKind
from app.domain.schedule import local_date
from app.models import Account, User
from app.services import busts, instance, ledger
from app.services.ledger import active_season_id

log = structlog.get_logger()
MAX_CATCH_UP_DAYS = 31


def allowance_key(user_id: int, day: date) -> str:
    return f"allowance:{user_id}:{day.isoformat()}"


def pay_due(engine: Engine, clock: Clock, tz: ZoneInfo, through: date) -> int:
    """Pay every allowance due up to and including `through`. Idempotent per user-day."""
    paid = 0
    with immediate(engine) as conn:
        config = instance.read(conn)
        season_id = active_season_id(conn)
        if config is None or season_id is None or not config.setup_completed:
            return 0
        amount = config.economy.daily_allowance_cents
        if amount <= 0 or config.state == instance.FROZEN:
            return 0
        accounts = conn.execute(
            select(Account.id, Account.user_id, Account.created_at)
            .join(User, User.id == Account.user_id)
            .where(
                Account.season_id == season_id,
                Account.kind == AccountKind.PLAYER.value,
                User.status != "banned",
            )
        ).all()
        first_allowed = through - timedelta(days=MAX_CATCH_UP_DAYS - 1)
        for account_id, user_id, created_at in accounts:
            if user_id is None:  # player accounts always have one (CHECK constraint)
                continue
            day = max(local_date(created_at, tz) + timedelta(days=1), first_allowed)
            while day <= through:
                result = ledger.pay_allowance(
                    conn, clock, account_id, amount, idempotency_key=allowance_key(user_id, day)
                )
                paid += not result.replayed
                day += timedelta(days=1)
        if paid:
            busts.check(conn, clock, season_id)
    log.info("allowance_paid", through=through.isoformat(), payments=paid)
    return paid
