"""Read models for the admin panel. Read-only; nothing here writes."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Connection, func, select

from app.domain.ledger import AccountKind
from app.models import (
    Account,
    AuditEntry,
    Bet,
    BetLeg,
    Bust,
    Command,
    Heartbeat,
    LedgerEntry,
    LedgerTxn,
    Market,
    OutboxMessage,
    SyncRun,
    User,
)
from app.services import busts, instance
from app.services.ledger import active_season_id

PAGE = 50


@dataclass(frozen=True, slots=True)
class PlayerRow:
    user_id: int
    display_name: str
    email: str
    status: str
    balance_cents: int | None
    pnl_cents: int | None
    open_bets: int
    busts: int
    active_bust_at: datetime | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class MarketRow:
    market_id: int
    title: str
    timeframe: str
    status: str
    lock_at: datetime
    bets: int
    staked_cents: int


def dashboard(conn: Connection, now: datetime) -> dict[str, Any]:
    season = active_season_id(conn)
    last_sync = conn.execute(
        select(SyncRun.status, SyncRun.finished_at, SyncRun.rows_new, SyncRun.error)
        .order_by(SyncRun.id.desc())
        .limit(1)
    ).one_or_none()
    heartbeat = conn.execute(
        select(Heartbeat.beat_at).where(Heartbeat.component == "worker")
    ).scalar_one_or_none()
    by_status = dict(
        conn.execute(
            select(Market.status, func.count())
            .where(Market.season_id == season)
            .group_by(Market.status)
        ).all()
    )
    exposure = conn.execute(
        select(
            func.count(),
            func.coalesce(func.sum(Bet.stake_cents), 0),
            func.coalesce(func.sum(Bet.potential_payout_cents), 0),
        ).where(Bet.status == "open", Bet.season_id == season)
    ).one()
    alerts = conn.execute(
        select(OutboxMessage.payload, OutboxMessage.created_at)
        .where(OutboxMessage.category == "admin_alerts")
        .order_by(OutboxMessage.id.desc())
        .limit(10)
    ).all()
    command = conn.execute(
        select(Command.status, Command.created_at, Command.done_at, Command.result)
        .where(Command.type == "sync_now")
        .order_by(Command.id.desc())
        .limit(1)
    ).one_or_none()
    return {
        "season": season,
        "last_sync": last_sync,
        "heartbeat": heartbeat,
        "worker_ok": heartbeat is not None and now - heartbeat < timedelta(minutes=3),
        "markets": by_status,
        "open_bets": exposure[0],
        "staked": exposure[1],
        "liability": exposure[2],
        "alerts": alerts,
        "command": command,
        "recent_audit": audit_page(conn, 0, limit=8),
    }


def markets(conn: Connection, status: str | None) -> list[MarketRow]:
    season = active_season_id(conn)
    bets = (
        select(
            BetLeg.market_id,
            func.count(BetLeg.id).label("n"),
            func.sum(Bet.stake_cents).label("staked"),
        )
        .join(Bet, Bet.id == BetLeg.bet_id)
        .group_by(BetLeg.market_id)
        .subquery()
    )
    query = (
        select(
            Market.id,
            Market.title,
            Market.timeframe,
            Market.status,
            Market.lock_at,
            func.coalesce(bets.c.n, 0),
            func.coalesce(bets.c.staked, 0),
        )
        .outerjoin(bets, bets.c.market_id == Market.id)
        .where(Market.season_id == season)
        .order_by(Market.lock_at.desc(), Market.id.desc())
        .limit(200)
    )
    if status:
        query = query.where(Market.status == status)
    return [MarketRow(*row) for row in conn.execute(query)]


def players(conn: Connection) -> list[PlayerRow]:
    season = active_season_id(conn)
    open_bets = (
        select(Bet.user_id, func.count().label("n"))
        .where(Bet.status == "open")
        .group_by(Bet.user_id)
        .subquery()
    )
    rows = conn.execute(
        select(
            User.id,
            User.display_name,
            User.email,
            User.status,
            Account.balance_cents,
            Account.pnl_cents,
            func.coalesce(open_bets.c.n, 0),
            User.created_at,
        )
        .outerjoin(
            Account,
            (Account.user_id == User.id)
            & (Account.season_id == season)
            & (Account.kind == AccountKind.PLAYER.value),
        )
        .outerjoin(open_bets, open_bets.c.user_id == User.id)
        .where(User.role == "player")
        .order_by(User.display_name)
    ).all()
    counts = busts.badge_counts(conn, season) if season else {}
    active = dict(
        conn.execute(
            select(Bust.user_id, Bust.busted_at).where(
                Bust.season_id == season, Bust.bailed_out_at.is_(None), Bust.resolved_at.is_(None)
            )
        ).all()
    )
    return [
        PlayerRow(
            user_id=r[0],
            display_name=r[1],
            email=r[2],
            status=r[3],
            balance_cents=r[4],
            pnl_cents=r[5],
            open_bets=r[6],
            busts=counts.get(r[0], 0),
            active_bust_at=active.get(r[0]),
            created_at=r[7],
        )
        for r in rows
    ]


def ledger_history(conn: Connection, user_id: int, limit: int = 100) -> list[Any]:
    return list(
        conn.execute(
            select(LedgerTxn.created_at, LedgerEntry.kind, LedgerEntry.amount_cents, LedgerTxn.memo)
            .join(LedgerTxn, LedgerTxn.id == LedgerEntry.txn_id)
            .join(Account, Account.id == LedgerEntry.account_id)
            .where(Account.user_id == user_id)
            .order_by(LedgerEntry.id.desc())
            .limit(limit)
        ).all()
    )


def bailout_ready_at(conn: Connection, row: PlayerRow) -> datetime | None:
    if row.active_bust_at is None:
        return None
    return row.active_bust_at + timedelta(days=instance.economy(conn).bailout_cooldown_days)


def audit_page(conn: Connection, page: int, limit: int = PAGE) -> list[Any]:
    actor = User.display_name.label("actor")
    return list(
        conn.execute(
            select(
                AuditEntry.ts,
                actor,
                AuditEntry.action,
                AuditEntry.target_type,
                AuditEntry.target_id,
                AuditEntry.reason,
                AuditEntry.before,
                AuditEntry.after,
            )
            .outerjoin(User, User.id == AuditEntry.actor_id)
            .order_by(AuditEntry.id.desc())
            .offset(page * limit)
            .limit(limit + 1)
        ).all()
    )
