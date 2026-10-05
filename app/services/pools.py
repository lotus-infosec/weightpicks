"""Special events: Price Is Right pools (BUILD_PLAN §1.4.2, D-010, D-043).

Players buy in once and guess the canonical weigh-in on the target date; the guess can be
changed for free until the lock (the night before, at the bet lock). Settlement waits for
the same readiness gate as markets: never on stale data. The pot moves through the pool's
own escrow account and always ends at zero.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import Connection, Engine, func, insert, select, update

from app.ai.text import problem
from app.core.clock import Clock
from app.core.db import immediate
from app.domain import pools as rules
from app.domain.ledger import AccountKind, InsufficientFunds
from app.domain.schedule import at_local, local_date
from app.domain.settlement import readiness
from app.models import Account, Pool, PoolEntry, User
from app.services import audit, instance, ledger
from app.services.instance import InstanceConfig
from app.services.observations import canonical_weigh_ins, latest_complete_through
from app.services.outbox import Category, enqueue
from app.services.settlement import last_ok_sync_finished_at

log = structlog.get_logger()

MIN_DAYS, MAX_DAYS = 2, 120  # target date, days from today
TITLE_MAX, QUESTION_MAX = 80, 300
MIN_GUESS_X10, MAX_GUESS_X10 = 300, 15_000  # 30.0 ... 1,500.0 in the instance unit
OPEN, LOCKED, SETTLED, REFUNDED = "open", "locked", "settled", "refunded"


class PoolError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class Draft:
    """A validated pool config, ready to preview and publish."""

    title: str
    question: str
    target_date: date
    buy_in_cents: int
    lock_at: datetime
    settle_after: datetime
    notes: tuple[str, ...] = ()  # what validation changed (e.g. buy-in clamped)

    def as_json(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "question": self.question,
            "target_date": self.target_date.isoformat(),
            "buy_in_cents": self.buy_in_cents,
        }


def latest_lock(config: InstanceConfig, target: date) -> datetime:
    """Pools lock the night before the target date at the bet lock (D-031, D-043)."""
    return at_local(target - timedelta(days=1), config.schedule.bet_lock, config.tz)


def validate(
    config: InstanceConfig,
    now: datetime,
    raw: Mapping[str, Any],
    *,
    names: list[str] | None = None,
) -> Draft:
    """Strict validation shared by the manual form and the AI draft. Raises PoolError."""
    title = " ".join(str(raw.get("title") or "").split())
    question = " ".join(str(raw.get("question") or "").split())
    if not title or len(title) > TITLE_MAX:
        raise PoolError("title", f"Give it a title of 1 to {TITLE_MAX} characters.")
    if not question or len(question) > QUESTION_MAX:
        raise PoolError("question", f"Write the question in 1 to {QUESTION_MAX} characters.")
    for text in (title, question):
        bad = problem(text, names or [])
        if bad:
            raise PoolError(
                "text", f"The title or question isn't allowed ({bad.replace('_', ' ')})."
            )
    try:
        target = date.fromisoformat(str(raw.get("target_date") or ""))
    except ValueError as exc:
        raise PoolError("target_date", "Pick the target date.") from exc
    today = local_date(now, config.tz)
    if not MIN_DAYS <= (target - today).days <= MAX_DAYS:
        raise PoolError(
            "target_date", f"The target date must be {MIN_DAYS} to {MAX_DAYS} days from today."
        )
    notes: list[str] = []
    try:
        buy_in = int(raw.get("buy_in_cents") or config.economy.pool_buyin_cents)
    except (TypeError, ValueError) as exc:
        raise PoolError("buy_in", "Enter the buy-in in dollars.") from exc
    clamped = config.economy.clamp_pool_buyin(buy_in)
    if clamped != buy_in:
        notes.append(f"Buy-in adjusted to ${clamped / 100:,.2f} (limit $1 to half the bankroll).")
    lock_at = latest_lock(config, target)
    settle_after = at_local(target, config.schedule.weigh_in_end, config.tz)
    if lock_at <= now:
        raise PoolError("target_date", "Too late to open that pool: it would lock already.")
    return Draft(title, question, target, clamped, lock_at, settle_after, tuple(notes))


def create(
    engine: Engine,
    clock: Clock,
    draft: Draft,
    *,
    actor_id: int | None,
    acted_at: datetime | None = None,
    ip: str | None = None,
    ai_run_id: int | None = None,
) -> int:
    with immediate(engine) as conn:
        if instance.current_state(conn) == instance.FROZEN:
            raise PoolError("frozen", "The season is frozen.")
        season_id = ledger.active_season_id(conn)
        if season_id is None:
            raise PoolError("no_season", "No season is running.")
        now = clock.now()
        if draft.lock_at <= now:
            raise PoolError("late", "Too late to open that pool: it would lock already.")
        escrow = ledger.open_pool_account(conn, clock, season_id)
        pool_id: int = conn.execute(
            insert(Pool)
            .values(
                season_id=season_id,
                account_id=escrow,
                title=draft.title,
                question=draft.question,
                rule="price_is_right",
                metric="weight",
                target_date=draft.target_date,
                buy_in_cents=draft.buy_in_cents,
                lock_at=draft.lock_at,
                settle_after=draft.settle_after,
                status=OPEN,
                config=draft.as_json(),
                ai_run_id=ai_run_id,
                created_by=actor_id,
                created_at=now,
            )
            .returning(Pool.id)
        ).scalar_one()
        enqueue(
            conn,
            clock,
            category=Category.SPECIAL_EVENTS,
            payload={"kind": "pool_open", "pool_id": pool_id} | draft.as_json(),
            dedupe_key=f"pool_open:{pool_id}",
        )
        audit.record(
            conn,
            clock,
            actor_id=actor_id,
            ts=acted_at,
            action="pool.create",
            target=("pool", pool_id),
            after=draft.as_json() | {"ai_run_id": ai_run_id},
            ip=ip,
        )
    log.info("pool_created", pool_id=pool_id, target_date=draft.target_date.isoformat())
    return pool_id


def parse_guess(raw: str) -> int:
    try:
        value = round(float(raw.replace(",", "").strip()) * 10)
    except ValueError as exc:
        raise PoolError("guess", "Enter your guess as a weight, like 212.4.") from exc
    if not MIN_GUESS_X10 <= value <= MAX_GUESS_X10:
        raise PoolError("guess", "That guess isn't a plausible weight.")
    return value


def enter(engine: Engine, clock: Clock, *, user_id: int, pool_id: int, guess_x10: int) -> bool:
    """Join a pool (pays the buy-in once) or change your guess before the lock. Returns
    True for a new entry, False for a changed guess."""
    if not MIN_GUESS_X10 <= guess_x10 <= MAX_GUESS_X10:
        raise PoolError("guess", "That guess isn't a plausible weight.")
    with immediate(engine) as conn:
        now = clock.now()
        user = conn.execute(select(User.role, User.status).where(User.id == user_id)).one_or_none()
        if user is None or user.role != "player" or user.status != "active":
            raise PoolError("user_inactive", "Your account can't enter pools right now.")
        if instance.current_state(conn) == instance.FROZEN:
            raise PoolError("frozen", "Betting is paused.")
        pool = conn.execute(select(Pool).where(Pool.id == pool_id)).one_or_none()
        if pool is None:
            raise PoolError("not_found", "No such pool.")
        if pool.status != OPEN or now >= pool.lock_at:
            raise PoolError("locked", "This pool is locked.")
        existing = conn.execute(
            select(PoolEntry.id).where(PoolEntry.pool_id == pool_id, PoolEntry.user_id == user_id)
        ).scalar_one_or_none()
        if existing is not None:
            conn.execute(
                update(PoolEntry)
                .where(PoolEntry.id == existing)
                .values(guess_x10=guess_x10, updated_at=now)
            )
            return False
        account_id = conn.execute(
            select(Account.id).where(
                Account.kind == AccountKind.PLAYER.value,
                Account.user_id == user_id,
                Account.season_id == pool.season_id,
            )
        ).scalar_one_or_none()
        if account_id is None:
            raise PoolError("no_account", "You don't have an account this season.")
        entry_id: int = conn.execute(
            insert(PoolEntry)
            .values(
                pool_id=pool_id,
                user_id=user_id,
                account_id=account_id,
                guess_x10=guess_x10,
                created_at=now,
                updated_at=now,
            )
            .returning(PoolEntry.id)
        ).scalar_one()
        try:
            ledger.pool_buyin(
                conn,
                clock,
                account_id,
                pool.account_id,
                pool.buy_in_cents,
                pool_id=pool_id,
                idempotency_key=f"pool:{pool_id}:buyin:{user_id}",
            )
        except InsufficientFunds as exc:
            raise PoolError("insufficient_funds", "Not enough balance for the buy-in.") from exc
    log.info("pool_entered", pool_id=pool_id, entry_id=entry_id)
    return True


# ---- lock, settle, refund ----------------------------------------------------------------


def lock_due(engine: Engine, now: datetime) -> int:
    with engine.connect() as conn:  # read first: most passes have nothing to lock
        if (
            conn.execute(select(Pool.id).where(Pool.status == OPEN, Pool.lock_at <= now)).first()
            is None
        ):
            return 0
    with immediate(engine) as conn:
        return conn.execute(
            update(Pool).where(Pool.status == OPEN, Pool.lock_at <= now).values(status=LOCKED)
        ).rowcount


def _entries(conn: Connection, pool_id: int) -> list[Any]:
    return list(
        conn.execute(
            select(PoolEntry.id, PoolEntry.user_id, PoolEntry.account_id, PoolEntry.guess_x10)
            .where(PoolEntry.pool_id == pool_id)
            .order_by(PoolEntry.created_at, PoolEntry.id)
        ).all()
    )


def _finish(
    conn: Connection, clock: Clock, pool: Any, outcome: rules.PoolOutcome, actual: int | None
) -> None:
    """Pay out or refund every entry, in the caller's write transaction."""
    entries = _entries(conn, pool.id)
    now = clock.now()
    if outcome.status == SETTLED:
        for entry in entries:
            cents = outcome.payouts.get(entry.id, 0)
            if cents:
                ledger.pool_payout(
                    conn,
                    clock,
                    entry.account_id,
                    pool.account_id,
                    cents,
                    pool_id=pool.id,
                    idempotency_key=f"pool:{pool.id}:payout:{entry.user_id}",
                )
            conn.execute(
                update(PoolEntry).where(PoolEntry.id == entry.id).values(payout_cents=cents)
            )
    else:
        for entry in entries:
            ledger.pool_refund(
                conn,
                clock,
                entry.account_id,
                pool.account_id,
                pool.buy_in_cents,
                pool_id=pool.id,
                idempotency_key=f"pool:{pool.id}:refund:{entry.user_id}",
            )
            conn.execute(
                update(PoolEntry)
                .where(PoolEntry.id == entry.id)
                .values(payout_cents=pool.buy_in_cents)
            )
    winners = [e.user_id for e in entries if e.id in outcome.winners]
    conn.execute(
        update(Pool)
        .where(Pool.id == pool.id)
        .values(
            status=outcome.status,
            result_x10=actual,
            outcome={"reason": outcome.reason, "winners": winners, "entries": len(entries)},
            settled_at=now,
        )
    )
    enqueue(
        conn,
        clock,
        category=Category.SPECIAL_EVENTS,
        payload={
            "kind": "pool_result",
            "pool_id": pool.id,
            "title": pool.title,
            "status": outcome.status,
            "reason": outcome.reason,
            "result_x10": actual,
            "winners": winners,
            "pot_cents": pool.buy_in_cents * len(entries),
            "share_cents": max(outcome.payouts.values(), default=0),
        },
        dedupe_key=f"pool_result:{pool.id}",
    )
    log.info("pool_finished", pool_id=pool.id, status=outcome.status, reason=outcome.reason)


def settle_pool(engine: Engine, clock: Clock, pool_id: int) -> str | None:
    """Settle one locked pool if its data is ready. Returns the new status, or None."""
    with immediate(engine) as conn:
        pool = conn.execute(select(Pool).where(Pool.id == pool_id)).one()
        if pool.status in (SETTLED, REFUNDED):
            return None
        now = clock.now()
        if pool.status == OPEN and now >= pool.lock_at:
            conn.execute(update(Pool).where(Pool.id == pool_id).values(status=LOCKED))
        elif pool.status != LOCKED:
            return None
        blocked = readiness(
            now=now,
            settle_after=pool.settle_after,
            window_end=pool.target_date,
            last_ok_sync_finished_at=last_ok_sync_finished_at(conn),
            complete_through=latest_complete_through(conn).get("weight"),
        )
        if blocked:
            return None
        canon = canonical_weigh_ins(conn, pool.target_date, pool.target_date)
        actual = canon[0].value if canon else None
        entries = _entries(conn, pool.id)
        outcome = rules.resolve(
            [(e.id, e.guess_x10) for e in entries], actual, pool.buy_in_cents * len(entries)
        )
        _finish(conn, clock, pool, outcome, actual)
        return outcome.status


def refund_pool(conn: Connection, clock: Clock, pool_id: int, reason: str) -> bool:
    """Refund every buy-in (admin void, Goal Reached). In the caller's transaction."""
    pool = conn.execute(select(Pool).where(Pool.id == pool_id)).one()
    if pool.status in (SETTLED, REFUNDED):
        return False
    _finish(conn, clock, pool, rules.PoolOutcome(REFUNDED, reason=reason), None)
    return True


def void(engine: Engine, clock: Clock, actor: Any, pool_id: int) -> None:
    """Admin void: refund every buy-in. Audited."""
    with immediate(engine) as conn:
        pool = conn.execute(select(Pool.status).where(Pool.id == pool_id)).one_or_none()
        if pool is None:
            raise PoolError("not_found", "No such pool.")
        if not refund_pool(conn, clock, pool_id, "admin_void"):
            raise PoolError("finished", f"This pool is already {pool.status}.")
        audit.record(
            conn,
            clock,
            actor_id=actor.user_id,
            ts=actor.acted_at,
            ip=actor.ip,
            action="pool.void",
            target=("pool", pool_id),
            before={"status": pool.status},
            after={"status": REFUNDED},
        )


@dataclass(frozen=True, slots=True)
class PoolPass:
    locked: int
    finished: int
    next_due: datetime | None


def tick(engine: Engine, clock: Clock) -> PoolPass:
    """Every-tick pass: lock due pools, then settle the ready ones. One read when idle."""
    now = clock.now()
    with engine.connect() as conn:
        live = conn.execute(
            select(Pool.id, Pool.status, Pool.lock_at, Pool.settle_after).where(
                Pool.status.in_((OPEN, LOCKED))
            )
        ).all()
    locked = (
        lock_due(engine, now) if any(p.status == OPEN and p.lock_at <= now for p in live) else 0
    )
    due = sorted(
        p.id for p in live if p.settle_after <= now and (p.status == LOCKED or p.lock_at <= now)
    )
    finished = sum(1 for pool_id in due if settle_pool(engine, clock, pool_id))
    upcoming = [p.lock_at for p in live if p.status == OPEN and p.lock_at > now] + [
        p.settle_after for p in live if p.settle_after > now
    ]
    return PoolPass(locked, finished, min(upcoming) if upcoming else None)


def open_entries(conn: Connection, account_id: int) -> int:
    """Open pool entries on an account (they keep a player from going bust)."""
    return int(
        conn.execute(
            select(func.count())
            .select_from(PoolEntry)
            .join(Pool, Pool.id == PoolEntry.pool_id)
            .where(PoolEntry.account_id == account_id, Pool.status.in_((OPEN, LOCKED)))
        ).scalar_one()
    )
