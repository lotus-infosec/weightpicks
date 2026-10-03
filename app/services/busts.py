"""Busts (A9, A11): a player is bust when their balance is under $1 and they have no
open bets. A bust stays active until a bailout or until the balance recovers another
way; every bust counts toward the season's shame badge and starts the bailout cooldown.
"""

from datetime import datetime

from sqlalchemy import Connection, exists, func, insert, select, update

from app.core.clock import Clock
from app.domain.ledger import AccountKind
from app.models import Account, Bet, Bust, User
from app.services.outbox import Category, enqueue

BUST_BELOW_CENTS = 100  # $1, the minimum bet (A9)


def check(conn: Connection, clock: Clock, season_id: int) -> list[int]:
    """Open busts for newly broke players; resolve active busts that recovered.
    Returns the new bust ids."""
    now = clock.now()
    open_bet = exists().where(Bet.account_id == Account.id, Bet.status == "open")
    active_for_user = exists().where(
        Bust.user_id == Account.user_id,
        Bust.season_id == season_id,
        Bust.bailed_out_at.is_(None),
        Bust.resolved_at.is_(None),
    )
    broke = conn.execute(
        select(Account.user_id, User.display_name)
        .join(User, User.id == Account.user_id)
        .where(
            Account.kind == AccountKind.PLAYER.value,
            Account.season_id == season_id,
            Account.balance_cents < BUST_BELOW_CENTS,
            User.status != "banned",
            ~open_bet,
            ~active_for_user,
        )
    ).all()
    created: list[int] = []
    for user_id, name in broke:
        bust_id = conn.execute(
            insert(Bust)
            .values(user_id=user_id, season_id=season_id, busted_at=now)
            .returning(Bust.id)
        ).scalar_one()
        count = conn.execute(
            select(func.count())
            .select_from(Bust)
            .where(Bust.user_id == user_id, Bust.season_id == season_id)
        ).scalar_one()
        enqueue(
            conn,
            clock,
            category=Category.BUSTS,
            payload={
                "bust_id": bust_id,
                "user_id": user_id,
                "display_name": name,
                "season_busts": count,
            },
            dedupe_key=f"bust:{bust_id}",
        )
        created.append(bust_id)
    recovered = select(Account.user_id).where(
        Account.kind == AccountKind.PLAYER.value,
        Account.season_id == season_id,
        Account.balance_cents >= BUST_BELOW_CENTS,
    )
    conn.execute(
        update(Bust)
        .where(
            Bust.season_id == season_id,
            Bust.bailed_out_at.is_(None),
            Bust.resolved_at.is_(None),
            Bust.user_id.in_(recovered),
        )
        .values(resolved_at=now)
    )
    return created


def active_bust(conn: Connection, user_id: int, season_id: int) -> tuple[int, datetime] | None:
    row = conn.execute(
        select(Bust.id, Bust.busted_at)
        .where(
            Bust.user_id == user_id,
            Bust.season_id == season_id,
            Bust.bailed_out_at.is_(None),
            Bust.resolved_at.is_(None),
        )
        .order_by(Bust.id.desc())
        .limit(1)
    ).one_or_none()
    return None if row is None else (row.id, row.busted_at)


def badge_counts(conn: Connection, season_id: int) -> dict[int, int]:
    return dict(
        conn.execute(
            select(Bust.user_id, func.count())
            .where(Bust.season_id == season_id)
            .group_by(Bust.user_id)
        ).all()
    )
