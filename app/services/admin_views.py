"""Read models for the admin panel. Read-only; nothing here writes."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

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
    Pool,
    SyncRun,
    User,
)
from app.services import admin_ai, ai_props, busts, instance
from app.services import props as props_service
from app.services.instance import InstanceConfig
from app.services.ledger import active_season_id
from app.services.observations import canonical_weigh_ins
from app.services.pools import entries_by_pool

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


def players(conn: Connection, user_id: int | None = None) -> list[PlayerRow]:
    """Every player (or just one) with this season's balance, P&L, open bets and busts."""
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
        .where(User.role == "player", *([User.id == user_id] if user_id is not None else []))
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


def recent_outbox(conn: Connection, limit: int = 10) -> list[Any]:
    """Latest Discord posts for the admin page: status and error only, never payload URLs."""
    return list(
        conn.execute(
            select(
                OutboxMessage.created_at,
                OutboxMessage.category,
                OutboxMessage.status,
                OutboxMessage.attempts,
                OutboxMessage.last_error,
            )
            .where(OutboxMessage.channel == "discord")
            .order_by(OutboxMessage.id.desc())
            .limit(limit)
        ).all()
    )


def pools(conn: Connection, limit: int = 30) -> list[dict[str, Any]]:
    """Special events, newest first, each with its entries (D-043)."""
    rows = conn.execute(select(Pool).order_by(Pool.id.desc()).limit(limit)).all()
    entries = entries_by_pool(conn, [p.id for p in rows])
    return [
        {"pool": p, "entries": entries[p.id], "pot_cents": p.buy_in_cents * len(entries[p.id])}
        for p in rows
    ]


def system_health(conn: Connection, now: datetime, cap: int) -> dict[str, Any]:
    """Admin -> System (BUILD_PLAN §2.9): data freshness, jobs, backlog, AI, integrity."""
    from app.ai import quota
    from app.models import AiRun, Heartbeat, JobRun, Settlement, SyncRun
    from app.services.observations import latest_complete_through

    last_ok = conn.execute(
        select(SyncRun.finished_at)
        .where(SyncRun.status == "ok")
        .order_by(SyncRun.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    last_failed = conn.execute(
        select(SyncRun.finished_at, SyncRun.error)
        .where(SyncRun.status != "ok")
        .order_by(SyncRun.id.desc())
        .limit(1)
    ).one_or_none()
    locked = conn.execute(select(func.count()).where(Market.status == "locked")).scalar_one()
    overdue = conn.execute(
        select(func.count()).where(Market.status == "locked", Market.settle_deadline < now)
    ).scalar_one()
    outbox = dict(
        conn.execute(
            select(OutboxMessage.status, func.count())
            .where(OutboxMessage.status.in_(("pending", "dead")))
            .group_by(OutboxMessage.status)
        ).all()
    )
    ledger_run = conn.execute(
        select(JobRun.status, JobRun.finished_at, JobRun.error)
        .where(JobRun.job == "ledger_verify")
        .order_by(JobRun.id.desc())
        .limit(1)
    ).one_or_none()
    backup_run = conn.execute(
        select(JobRun.status, JobRun.finished_at, JobRun.error)
        .where(JobRun.job == "auto_backup")
        .order_by(JobRun.id.desc())
        .limit(1)
    ).one_or_none()
    beats = conn.execute(select(Heartbeat.component, Heartbeat.beat_at)).all()
    ai_failures = conn.execute(
        select(func.count()).where(
            AiRun.status == "error", AiRun.started_at >= now - timedelta(days=1)
        )
    ).scalar_one()
    return {
        "last_ok_sync": last_ok,
        "last_failed_sync": last_failed,
        "complete_through": latest_complete_through(conn),
        "last_drop": conn.execute(
            select(func.max(Market.created_at)).where(Market.origin == "core")
        ).scalar_one(),
        "last_settlement": conn.execute(select(func.max(Settlement.created_at))).scalar_one(),
        "locked": int(locked),
        "overdue": int(overdue),
        "outbox_pending": int(outbox.get("pending", 0)),
        "outbox_dead": int(outbox.get("dead", 0)),
        "neurons_used": quota.used_today(conn, now),
        "neurons_cap": cap,
        "ai_failures_24h": int(ai_failures),
        "ledger_run": ledger_run,
        "backup_run": backup_run,
        "heartbeats": [(c, b, now - b < timedelta(minutes=3)) for c, b in beats],
    }


@dataclass(frozen=True, slots=True)
class PropsData:
    config: InstanceConfig | None
    today: date
    latest_x10: int | None  # the latest weigh-in of the last two weeks
    open_props: list[Any]
    ai_queue: list[dict[str, Any]]  # pending AI proposals, each re-priced now
    ai_last_run: Any
    ai_configured: bool


def props(conn: Connection, now: datetime, default_tz: ZoneInfo) -> PropsData:
    """Everything the admin Props page shows (D-041, D-042)."""
    config = instance.read(conn)
    today = now.astimezone(config.tz if config else default_tz).date()
    canon = canonical_weigh_ins(conn, today - timedelta(days=13), today)
    open_props = list(
        conn.execute(
            select(Market.id.label("market_id"), Market.title, Market.status)
            .where(Market.origin.in_(("admin", "ai")), Market.status.in_(("open", "locked")))
            .order_by(Market.id.desc())
            .limit(30)
        ).all()
    )
    ai_queue: list[dict[str, Any]] = []
    for proposal in ai_props.pending(conn, now):
        if config is None:
            break
        try:
            priced = props_service.preview(
                conn, config, today, proposal.template, dict(proposal.form)
            )
        except props_service.PropError:
            priced = None
        ai_queue.append({"row": proposal, "preview": priced})
    last_run = conn.execute(
        select(Command.status, Command.result, Command.created_at)
        .where(Command.type == "ai_props_now")
        .order_by(Command.id.desc())
        .limit(1)
    ).one_or_none()
    return PropsData(
        config,
        today,
        canon[-1].value if canon else None,
        open_props,
        ai_queue,
        last_run,
        admin_ai.workers_ai_configured(conn),
    )
