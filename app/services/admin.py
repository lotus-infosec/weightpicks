"""Admin actions (BUILD_PLAN §1.5, CONCEPT §8, D-012, D-037). Each action is one short
BEGIN IMMEDIATE transaction that also writes the audit row, so an action and its audit
entry can never disagree. Password re-prompts are enforced by the web layer.
"""

import secrets as pysecrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import Connection, Engine, insert, select, update

from app.core.clock import Clock
from app.core.db import immediate
from app.core.security import hash_password
from app.domain import setup as setup_steps
from app.domain.economy import Economy
from app.domain.ledger import AccountKind, InsufficientFunds
from app.domain.markets import MarketStatus
from app.models import (
    Account,
    BannedEmail,
    Bet,
    BetLeg,
    Bust,
    Command,
    InstanceSettingsRow,
    Market,
    User,
)
from app.services import audit, auth, busts, ledger
from app.services import instance as instance_service
from app.services import secrets as secret_store
from app.services.markets import set_status
from app.services.outbox import Category, enqueue
from app.services.settlement import reevaluate_parlay

log = structlog.get_logger()


class AdminError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class Actor:
    user_id: int
    ip: str | None = None
    acted_at: datetime | None = (
        None  # real time of the click; audit rows use it (dev: SimClock differs)
    )


def _player(conn: Connection, user_id: int) -> Any:
    row = conn.execute(
        select(User.id, User.role, User.status, User.email, User.display_name).where(
            User.id == user_id
        )
    ).one_or_none()
    if row is None:
        raise AdminError("not_found", "No such user.")
    if row.role != "player":
        raise AdminError("not_player", "That action only applies to players.")
    return row


def _account(conn: Connection, user_id: int) -> tuple[int, int, int]:
    season = ledger.active_season_id(conn)
    row = conn.execute(
        select(Account.id, Account.balance_cents, Account.season_id).where(
            Account.kind == AccountKind.PLAYER.value,
            Account.user_id == user_id,
            Account.season_id == season,
        )
    ).one_or_none()
    if row is None:
        raise AdminError("no_account", "That player has no account this season.")
    return row.id, row.balance_cents, row.season_id


def _void_bets(conn: Connection, clock: Clock, bet_rows: list[Any], reason: str) -> int:
    """Void and refund open bets (single legs); outbox one result per bet."""
    for bet in bet_rows:
        ledger.refund(
            conn,
            clock,
            bet.account_id,
            bet.stake_cents,
            idempotency_key=f"bet:{bet.id}:refund",
            ref_id=bet.id,
        )
        conn.execute(update(BetLeg).where(BetLeg.bet_id == bet.id).values(status="void"))
        conn.execute(
            update(Bet)
            .where(Bet.id == bet.id)
            .values(status="void", payout_cents=bet.stake_cents, settled_at=clock.now())
        )
        enqueue(
            conn,
            clock,
            category=Category.BET_RESULTS,
            payload={
                "bet_id": bet.id,
                "user_id": bet.user_id,
                "result": "void",
                "reason": reason,
                "stake_cents": bet.stake_cents,
                "payout_cents": bet.stake_cents,
            },
            dedupe_key=f"bet_result:{bet.id}",
        )
    return len(bet_rows)


# ---- markets ----------------------------------------------------------------------------


def void_open_market(conn: Connection, clock: Clock, market_id: int, reason: str) -> int:
    """Void one open or locked market in the caller's transaction: refund its single bets,
    void its parlay legs (each parlay is re-evaluated) and post the result. Returns bets
    voided. Shared by the admin's void and Goal Reached (D-011)."""
    market = conn.execute(select(Market.status, Market.title).where(Market.id == market_id)).one()
    bets = conn.execute(
        select(Bet.id, Bet.account_id, Bet.user_id, Bet.stake_cents)
        .join(BetLeg, BetLeg.bet_id == Bet.id)
        .where(BetLeg.market_id == market_id, BetLeg.status == "open", Bet.kind == "single")
        .order_by(Bet.id)
    ).all()
    voided = _void_bets(conn, clock, list(bets), reason)
    # In a parlay only this leg is voided: it drops out and the parlay is re-evaluated.
    parlay_legs = conn.execute(
        select(BetLeg.id, BetLeg.bet_id)
        .join(Bet, Bet.id == BetLeg.bet_id)
        .where(BetLeg.market_id == market_id, BetLeg.status == "open", Bet.kind == "parlay")
        .order_by(BetLeg.id)
    ).all()
    for leg_id, bet_id in parlay_legs:
        conn.execute(update(BetLeg).where(BetLeg.id == leg_id).values(status="void"))
        reevaluate_parlay(conn, clock, bet_id, clock.now())
    voided += len(parlay_legs)
    set_status(conn, [market_id], MarketStatus(market.status), MarketStatus.VOIDED, clock.now())
    enqueue(
        conn,
        clock,
        category=Category.MARKET_SETTLEMENTS,
        payload={
            "market_id": market_id,
            "title": market.title,
            "result": "void",
            "reason": reason,
        },
        dedupe_key=f"market_voided:{market_id}",
    )
    return voided


def void_market(engine: Engine, clock: Clock, actor: Actor, market_id: int, reason: str) -> int:
    """Void an open or locked market and refund every open bet on it. Returns bets voided;
    voiding an already voided market is a no-op (0)."""
    reason = reason.strip()
    if not reason:
        raise AdminError("reason_required", "Give a reason for voiding.")
    with immediate(engine) as conn:
        market = conn.execute(
            select(Market.status, Market.title, Market.season_id).where(Market.id == market_id)
        ).one_or_none()
        if market is None:
            raise AdminError("not_found", "No such market.")
        if market.status == MarketStatus.VOIDED.value:
            return 0
        if market.status not in (MarketStatus.OPEN.value, MarketStatus.LOCKED.value):
            raise AdminError("not_voidable", f"A {market.status} market can't be voided.")
        voided = void_open_market(conn, clock, market_id, "market voided")
        audit.record(
            conn,
            clock,
            actor_id=actor.user_id,
            ts=actor.acted_at,
            action="market.void",
            target=("market", market_id),
            before={"status": market.status},
            after={"status": "voided", "bets_refunded": voided},
            reason=reason,
            ip=actor.ip,
        )
        busts.check(conn, clock, market.season_id)
    log.info("market_voided", market_id=market_id, bets=voided)
    return voided


# ---- users --------------------------------------------------------------------------------


def set_frozen(engine: Engine, clock: Clock, actor: Actor, user_id: int, frozen: bool) -> None:
    with immediate(engine) as conn:
        user = _player(conn, user_id)
        if user.status == "banned":
            raise AdminError("banned", "That player has been removed.")
        new = "frozen" if frozen else "active"
        if user.status == new:
            return
        conn.execute(update(User).where(User.id == user_id).values(status=new))
        audit.record(
            conn,
            clock,
            actor_id=actor.user_id,
            ts=actor.acted_at,
            action="user.freeze" if frozen else "user.unfreeze",
            target=("user", user_id),
            before={"status": user.status},
            after={"status": new},
            ip=actor.ip,
        )


def ban(engine: Engine, clock: Clock, actor: Actor, user_id: int, reason: str) -> int:
    """D-012: remove a player. Sessions end, the email is blocked, open bets are voided
    and refunded; history stays (shown as a removed player). Returns bets refunded."""
    reason = reason.strip()
    if not reason:
        raise AdminError("reason_required", "Give a reason for the ban.")
    with immediate(engine) as conn:
        user = _player(conn, user_id)
        if user.status == "banned":
            return 0
        bets = conn.execute(
            select(Bet.id, Bet.account_id, Bet.user_id, Bet.stake_cents)
            .where(Bet.user_id == user_id, Bet.status == "open")
            .order_by(Bet.id)
        ).all()
        refunded = _void_bets(conn, clock, list(bets), "player removed")
        conn.execute(update(User).where(User.id == user_id).values(status="banned"))
        sessions = auth.revoke_all(conn, user_id)
        if not conn.execute(
            select(BannedEmail.email).where(BannedEmail.email == user.email)
        ).first():
            conn.execute(
                insert(BannedEmail).values(
                    email=user.email, banned_at=clock.now(), reason=reason[:200]
                )
            )
        audit.record(
            conn,
            clock,
            actor_id=actor.user_id,
            ts=actor.acted_at,
            action="user.ban",
            target=("user", user_id),
            before={"status": user.status},
            after={"status": "banned", "bets_refunded": refunded, "sessions_ended": sessions},
            reason=reason,
            ip=actor.ip,
        )
    log.info("user_banned", user_id=user_id, bets_refunded=refunded)
    return refunded


def reset_password(engine: Engine, clock: Clock, actor: Actor, user_id: int) -> str:
    """Set a random temporary password (shown to the admin once) and end all sessions."""
    temporary = pysecrets.token_urlsafe(9)  # 12 characters
    password_hash = hash_password(temporary)
    with immediate(engine) as conn:
        user = _player(conn, user_id)
        if user.status == "banned":
            raise AdminError("banned", "That player has been removed.")
        conn.execute(update(User).where(User.id == user_id).values(password_hash=password_hash))
        sessions = auth.revoke_all(conn, user_id)
        audit.record(
            conn,
            clock,
            actor_id=actor.user_id,
            ts=actor.acted_at,
            action="user.reset_password",
            target=("user", user_id),
            after={"sessions_ended": sessions},
            ip=actor.ip,
        )
    return temporary


# ---- bank ---------------------------------------------------------------------------------


def bailout(engine: Engine, clock: Clock, actor: Actor, user_id: int) -> int:
    """D-037: only for a player who is bust, once the post-bust cooldown has passed.
    Returns the amount paid. Bailouts never touch P&L (D-007)."""
    now = clock.now()
    with immediate(engine) as conn:
        _player(conn, user_id)
        account_id, balance, season_id = _account(conn, user_id)
        busts.check(conn, clock, season_id)
        active = busts.active_bust(conn, user_id, season_id)
        if active is None:
            raise AdminError("not_busted", "Bailouts are only for players who are bust.")
        bust_id, busted_at = active
        economy = instance_service.economy(conn)
        ready_at = busted_at + timedelta(days=economy.bailout_cooldown_days)
        if now < ready_at:
            raise AdminError(
                "cooldown", f"Bailouts open {economy.bailout_cooldown_days} days after going bust."
            )
        ledger.bailout(
            conn,
            clock,
            account_id,
            economy.bailout_cents,
            idempotency_key=f"bailout:bust{bust_id}",
            created_by=actor.user_id,
        )
        conn.execute(update(Bust).where(Bust.id == bust_id).values(bailed_out_at=now))
        audit.record(
            conn,
            clock,
            actor_id=actor.user_id,
            ts=actor.acted_at,
            action="bank.bailout",
            target=("user", user_id),
            before={"balance_cents": balance},
            after={"balance_cents": balance + economy.bailout_cents, "bust_id": bust_id},
            ip=actor.ip,
        )
    return economy.bailout_cents


def adjust(
    engine: Engine, clock: Clock, actor: Actor, user_id: int, amount_cents: int, reason: str
) -> None:
    reason = reason.strip()
    if not reason:
        raise AdminError("reason_required", "Give a reason for the adjustment.")
    if amount_cents == 0:
        raise AdminError("zero", "Enter a non-zero amount.")
    with immediate(engine) as conn:
        _player(conn, user_id)
        account_id, balance, season_id = _account(conn, user_id)
        key = f"adjust:{user_id}:{clock.now().isoformat()}:{pysecrets.token_hex(4)}"
        try:
            ledger.admin_adjust(
                conn,
                clock,
                account_id,
                amount_cents,
                reason=reason,
                created_by=actor.user_id,
                idempotency_key=key,
            )
        except InsufficientFunds as exc:
            raise AdminError("insufficient_funds", "That would take the balance below $0.") from exc
        audit.record(
            conn,
            clock,
            actor_id=actor.user_id,
            ts=actor.acted_at,
            action="bank.adjust",
            target=("user", user_id),
            before={"balance_cents": balance},
            after={"balance_cents": balance + amount_cents, "amount_cents": amount_cents},
            reason=reason,
            ip=actor.ip,
        )
        busts.check(conn, clock, season_id)


def update_economy(engine: Engine, clock: Clock, actor: Actor, economy: Economy) -> None:
    with immediate(engine) as conn:
        before = instance_service.economy(conn)
        if before == economy:
            return
        conn.execute(
            update(InstanceSettingsRow)
            .where(InstanceSettingsRow.id == 1)
            .values(economy=economy.to_json(), updated_at=clock.now())
        )
        audit.record(
            conn,
            clock,
            actor_id=actor.user_id,
            ts=actor.acted_at,
            action="settings.economy",
            target=("settings", 1),
            before=before.to_json(),
            after=economy.to_json(),
            ip=actor.ip,
        )


def rotate_registration_code(engine: Engine, clock: Clock, actor: Actor) -> str:
    code = auth.rotate_registration_code(engine, clock)
    with immediate(engine) as conn:
        audit.record(
            conn,
            clock,
            actor_id=actor.user_id,
            ts=actor.acted_at,
            action="registration.rotate",
            ip=actor.ip,
        )
    return code


def reauth_failed(engine: Engine, clock: Clock, actor: Actor, action: str) -> None:
    with immediate(engine) as conn:
        audit.record(
            conn,
            clock,
            actor_id=actor.user_id,
            ts=actor.acted_at,
            action="reauth_failed",
            after={"for": action},
            ip=actor.ip,
        )


# ---- commands -----------------------------------------------------------------------------


COMMANDS = {"sync_now": "sync.request", "ai_props_now": "ai.run_request"}


def request_command(engine: Engine, clock: Clock, actor: Actor, kind: str) -> int:
    """Queue a command for the worker (one pending of each kind at a time)."""
    action = COMMANDS[kind]
    with immediate(engine) as conn:
        pending = conn.execute(
            select(Command.id).where(
                Command.type == kind, Command.status.in_(("pending", "running"))
            )
        ).scalar_one_or_none()
        if pending is not None:
            return int(pending)
        command_id: int = conn.execute(
            insert(Command)
            .values(
                type=kind,
                args={},
                status="pending",
                created_by=actor.user_id,
                created_at=clock.now(),
            )
            .returning(Command.id)
        ).scalar_one()
        audit.record(
            conn,
            clock,
            actor_id=actor.user_id,
            ts=actor.acted_at,
            action=action,
            target=("command", command_id),
            ip=actor.ip,
        )
    return command_id


def request_sync(engine: Engine, clock: Clock, actor: Actor) -> int:
    """Queue a sync-now command for the worker (one pending at a time)."""
    return request_command(engine, clock, actor, "sync_now")


__all__ = ["Actor", "AdminError"]


# ---- Discord and flags (STAGE11, D-040) ----------------------------------------------

ADMIN_FLAGS = (  # /admin/discord (Settings)
    "registration_open",
    "discord_public",
    "props_futures",
    "parlays",
    "ai_props",
    "ai_hype",
)


def set_webhook(
    engine: Engine, clock: Clock, actor: Actor, app_secret_key: str, category: str, url: str
) -> None:
    """Store (or, with an empty url, clear) one category's webhook, encrypted. The audit
    row says what changed, never the URL."""
    if category not in secret_store.WEBHOOK_CATEGORIES:
        raise AdminError("unknown_category", "Unknown Discord category.")
    url = url.strip()
    if url:
        problem = setup_steps.webhook_problem(url)
        if problem:
            raise AdminError("bad_webhook", problem)
        if not app_secret_key:
            raise AdminError("no_key", "APP_SECRET_KEY isn't set, so webhooks can't be stored.")
    name = f"webhook.{category}"
    with immediate(engine) as conn:
        had = name in secret_store.names(conn)
        if url:
            secret_store.put(conn, clock, app_secret_key, name, url)
        elif had:
            secret_store.remove(conn, name)
        else:
            return
        audit.record(
            conn,
            clock,
            actor_id=actor.user_id,
            ts=actor.acted_at,
            action="discord.webhook_set" if url else "discord.webhook_cleared",
            target=None,
            before={"category": category, "set": had},
            after={"category": category, "set": bool(url)},
            ip=actor.ip,
        )


def send_test(engine: Engine, domain_clock: Clock, actor: Actor, category: str) -> None:
    if category not in secret_store.WEBHOOK_CATEGORIES:
        raise AdminError("unknown_category", "Unknown Discord category.")
    with immediate(engine) as conn:
        enqueue(
            conn,
            domain_clock,
            category=Category(category),
            payload={"kind": "test"},
            dedupe_key=f"test:{category}:{(actor.acted_at or domain_clock.now()).isoformat()}",
        )


def set_flag(engine: Engine, clock: Clock, actor: Actor, flag: str, value: bool) -> None:
    if flag not in ADMIN_FLAGS:
        raise AdminError("unknown_flag", "That setting can't be changed here.")
    with immediate(engine) as conn:
        flags = dict(conn.execute(select(InstanceSettingsRow.flags)).scalar_one() or {})
        before = bool(flags.get(flag, False))
        if before == value:
            return
        flags[flag] = value
        conn.execute(
            update(InstanceSettingsRow)
            .where(InstanceSettingsRow.id == 1)
            .values(flags=flags, updated_at=clock.now())
        )
        audit.record(
            conn,
            clock,
            actor_id=actor.user_id,
            ts=actor.acted_at,
            action="settings.flag",
            target=("settings", 1),
            before={flag: before},
            after={flag: value},
            ip=actor.ip,
        )
