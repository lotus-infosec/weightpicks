"""Posting money: the only write path to the ledger (BUILD_PLAN §1.3, D-007, D-026).

Every function takes a `Connection` that is already inside `immediate()` so callers
can combine a business change (a bet, a pool entry) and its ledger transaction in
one atomic write. Rules enforced here, with database backstops in migration 0002:
entries balance to zero, player balances never go below zero, a transaction stays
inside one season, and an idempotency key maps to exactly one transaction.
"""

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy import Connection, case, func, insert, select, update

from app.core.clock import Clock
from app.domain.ledger import (
    BETTING_KINDS,
    AccountKind,
    AccountNotFound,
    CrossSeasonTxn,
    Entry,
    EntryKind,
    IdempotencyConflict,
    InsufficientFunds,
    fingerprint,
    validate_entries,
)
from app.models import Account, LedgerEntry, LedgerTxn, Season


@dataclass(frozen=True, slots=True)
class PostResult:
    txn_id: int
    replayed: bool


# ---- seasons and accounts --------------------------------------------------------


def active_season_id(conn: Connection) -> int | None:
    return conn.execute(select(Season.id).where(Season.ended_at.is_(None))).scalar_one_or_none()


def open_season(conn: Connection, clock: Clock) -> int:
    """Start the next season with its `mint` and `house` accounts."""
    number = conn.execute(select(func.coalesce(func.max(Season.number), 0))).scalar_one() + 1
    now = clock.now()
    season_id = conn.execute(
        insert(Season).values(number=number, status="active", started_at=now).returning(Season.id)
    ).scalar_one()
    for kind in (AccountKind.MINT, AccountKind.HOUSE):
        conn.execute(
            insert(Account).values(
                kind=kind.value, season_id=season_id, balance_cents=0, pnl_cents=0, created_at=now
            )
        )
    return season_id


def system_account(conn: Connection, season_id: int, kind: AccountKind) -> int:
    account_id = conn.execute(
        select(Account.id).where(Account.season_id == season_id, Account.kind == kind.value)
    ).scalar_one_or_none()
    if account_id is None:
        raise AccountNotFound(f"no {kind.value} account for season {season_id}")
    return account_id


def open_player_account(conn: Connection, clock: Clock, season_id: int, user_id: int) -> int:
    """The player's account for a season, created on first use."""
    existing = conn.execute(
        select(Account.id).where(
            Account.season_id == season_id,
            Account.user_id == user_id,
            Account.kind == AccountKind.PLAYER.value,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    return conn.execute(
        insert(Account)
        .values(
            kind=AccountKind.PLAYER.value,
            user_id=user_id,
            season_id=season_id,
            balance_cents=0,
            pnl_cents=0,
            created_at=clock.now(),
        )
        .returning(Account.id)
    ).scalar_one()


# ---- the one write path ----------------------------------------------------------


def post_txn(
    conn: Connection,
    clock: Clock,
    *,
    kind: EntryKind,
    entries: Sequence[Entry],
    idempotency_key: str,
    ref_type: str | None = None,
    ref_id: int | None = None,
    created_by: int | None = None,
    memo: str | None = None,
) -> PostResult:
    validate_entries(entries)
    digest = fingerprint(
        kind, entries, ref_type=ref_type, ref_id=ref_id, memo=memo, created_by=created_by
    )

    existing = conn.execute(
        select(LedgerTxn.id, LedgerTxn.fingerprint).where(
            LedgerTxn.idempotency_key == idempotency_key
        )
    ).one_or_none()
    if existing is not None:
        if existing.fingerprint != digest:
            raise IdempotencyConflict(
                f"idempotency key {idempotency_key!r} was used for different content"
            )
        return PostResult(txn_id=existing.id, replayed=True)

    deltas: dict[int, int] = defaultdict(int)
    pnl_deltas: dict[int, int] = defaultdict(int)
    for entry in entries:
        deltas[entry.account_id] += entry.amount_cents
        if entry.kind in BETTING_KINDS:
            pnl_deltas[entry.account_id] += entry.amount_cents

    accounts = {
        row.id: row
        for row in conn.execute(
            select(Account.id, Account.kind, Account.season_id, Account.balance_cents).where(
                Account.id.in_(deltas)
            )
        )
    }
    missing = sorted(set(deltas) - set(accounts))
    if missing:
        raise AccountNotFound(f"unknown account ids {missing}")
    if len({row.season_id for row in accounts.values()}) != 1:
        raise CrossSeasonTxn("all entries of a transaction must belong to one season")
    for account_id, delta in deltas.items():
        row = accounts[account_id]
        if row.kind == AccountKind.PLAYER.value and row.balance_cents + delta < 0:
            raise InsufficientFunds(
                f"account {account_id} has {row.balance_cents} cents, needs {-delta}"
            )

    txn_id = conn.execute(
        insert(LedgerTxn)
        .values(
            kind=kind.value,
            ref_type=ref_type,
            ref_id=ref_id,
            created_by=created_by,
            memo=memo,
            created_at=clock.now(),
            idempotency_key=idempotency_key,
            fingerprint=digest,
        )
        .returning(LedgerTxn.id)
    ).scalar_one()
    conn.execute(
        insert(LedgerEntry),
        [
            {
                "txn_id": txn_id,
                "account_id": e.account_id,
                "amount_cents": e.amount_cents,
                "kind": e.kind.value,
            }
            for e in entries
        ],
    )
    for account_id, delta in deltas.items():
        conn.execute(
            update(Account)
            .where(Account.id == account_id)
            .values(
                balance_cents=Account.balance_cents + delta,
                pnl_cents=Account.pnl_cents + pnl_deltas[account_id],
            )
        )
    return PostResult(txn_id=txn_id, replayed=False)


# ---- primitives -----------------------------------------------------------------------


def _season_of(conn: Connection, account_id: int) -> int:
    season_id = conn.execute(
        select(Account.season_id).where(Account.id == account_id)
    ).scalar_one_or_none()
    if season_id is None:
        raise AccountNotFound(f"unknown account id {account_id}")
    return season_id


def _require_positive(amount_cents: int) -> None:
    if amount_cents <= 0:
        raise ValueError(f"amount must be positive, got {amount_cents}")


def _transfer(
    conn: Connection,
    clock: Clock,
    *,
    kind: EntryKind,
    source: AccountKind,
    player_account_id: int,
    amount_cents: int,
    idempotency_key: str,
    ref_type: str | None = None,
    ref_id: int | None = None,
    created_by: int | None = None,
    memo: str | None = None,
) -> PostResult:
    """Move `amount_cents` from a season system account to the player (negative = back)."""
    counterpart = system_account(conn, _season_of(conn, player_account_id), source)
    return post_txn(
        conn,
        clock,
        kind=kind,
        entries=[
            Entry(counterpart, -amount_cents, kind),
            Entry(player_account_id, amount_cents, kind),
        ],
        idempotency_key=idempotency_key,
        ref_type=ref_type,
        ref_id=ref_id,
        created_by=created_by,
        memo=memo,
    )


def grant_starting(
    conn: Connection, clock: Clock, account_id: int, amount_cents: int, *, idempotency_key: str
) -> PostResult:
    _require_positive(amount_cents)
    return _transfer(
        conn,
        clock,
        kind=EntryKind.STARTING_GRANT,
        source=AccountKind.MINT,
        player_account_id=account_id,
        amount_cents=amount_cents,
        idempotency_key=idempotency_key,
    )


def pay_allowance(
    conn: Connection, clock: Clock, account_id: int, amount_cents: int, *, idempotency_key: str
) -> PostResult:
    _require_positive(amount_cents)
    return _transfer(
        conn,
        clock,
        kind=EntryKind.ALLOWANCE,
        source=AccountKind.MINT,
        player_account_id=account_id,
        amount_cents=amount_cents,
        idempotency_key=idempotency_key,
    )


def bailout(
    conn: Connection,
    clock: Clock,
    account_id: int,
    amount_cents: int,
    *,
    idempotency_key: str,
    created_by: int | None = None,
) -> PostResult:
    """Ledger posting only; the post-bust cooldown rule is enforced by the admin service."""
    _require_positive(amount_cents)
    return _transfer(
        conn,
        clock,
        kind=EntryKind.BAILOUT,
        source=AccountKind.MINT,
        player_account_id=account_id,
        amount_cents=amount_cents,
        idempotency_key=idempotency_key,
        created_by=created_by,
    )


def admin_adjust(
    conn: Connection,
    clock: Clock,
    account_id: int,
    amount_cents: int,
    *,
    reason: str,
    created_by: int | None,
    idempotency_key: str,
) -> PostResult:
    """Signed correction against the mint. A reason is mandatory and stored as the memo."""
    if amount_cents == 0:
        raise ValueError("adjustment must not be zero")
    if not reason.strip():
        raise ValueError("an admin adjustment needs a reason")
    return _transfer(
        conn,
        clock,
        kind=EntryKind.ADMIN_ADJUST,
        source=AccountKind.MINT,
        player_account_id=account_id,
        amount_cents=amount_cents,
        idempotency_key=idempotency_key,
        created_by=created_by,
        memo=reason.strip(),
    )


def stake(
    conn: Connection,
    clock: Clock,
    account_id: int,
    amount_cents: int,
    *,
    idempotency_key: str,
    ref_type: str = "bet",
    ref_id: int | None = None,
) -> PostResult:
    _require_positive(amount_cents)
    return _transfer(
        conn,
        clock,
        kind=EntryKind.BET_STAKE,
        source=AccountKind.HOUSE,
        player_account_id=account_id,
        amount_cents=-amount_cents,
        idempotency_key=idempotency_key,
        ref_type=ref_type,
        ref_id=ref_id,
    )


def payout(
    conn: Connection,
    clock: Clock,
    account_id: int,
    amount_cents: int,
    *,
    idempotency_key: str,
    ref_type: str = "bet",
    ref_id: int | None = None,
) -> PostResult:
    """Total return on a winning bet (stake + profit), paid by the house."""
    _require_positive(amount_cents)
    return _transfer(
        conn,
        clock,
        kind=EntryKind.BET_PAYOUT,
        source=AccountKind.HOUSE,
        player_account_id=account_id,
        amount_cents=amount_cents,
        idempotency_key=idempotency_key,
        ref_type=ref_type,
        ref_id=ref_id,
    )


def refund(
    conn: Connection,
    clock: Clock,
    account_id: int,
    amount_cents: int,
    *,
    idempotency_key: str,
    ref_type: str = "bet",
    ref_id: int | None = None,
) -> PostResult:
    """Return of a stake (push or void), paid by the house."""
    _require_positive(amount_cents)
    return _transfer(
        conn,
        clock,
        kind=EntryKind.BET_REFUND,
        source=AccountKind.HOUSE,
        player_account_id=account_id,
        amount_cents=amount_cents,
        idempotency_key=idempotency_key,
        ref_type=ref_type,
        ref_id=ref_id,
    )


def carry_over(
    conn: Connection, clock: Clock, account_id: int, amount_cents: int, *, idempotency_key: str
) -> PostResult:
    """Last season's ending balance, minted into the player's new-season account. Not a
    betting kind, so the new season's P&L starts at 0 (D-043)."""
    _require_positive(amount_cents)
    return _transfer(
        conn,
        clock,
        kind=EntryKind.SEASON_CARRY,
        source=AccountKind.MINT,
        player_account_id=account_id,
        amount_cents=amount_cents,
        idempotency_key=idempotency_key,
    )


# ---- pools (special events) -----------------------------------------------------------


def open_pool_account(conn: Connection, clock: Clock, season_id: int) -> int:
    """A fresh escrow account for one pool."""
    account_id: int = conn.execute(
        insert(Account)
        .values(
            kind=AccountKind.POOL.value,
            season_id=season_id,
            balance_cents=0,
            pnl_cents=0,
            created_at=clock.now(),
        )
        .returning(Account.id)
    ).scalar_one()
    return account_id


def _pool_move(
    conn: Connection,
    clock: Clock,
    *,
    kind: EntryKind,
    escrow_account_id: int,
    player_account_id: int,
    to_player_cents: int,
    idempotency_key: str,
    pool_id: int,
) -> PostResult:
    return post_txn(
        conn,
        clock,
        kind=kind,
        entries=[
            Entry(escrow_account_id, -to_player_cents, kind),
            Entry(player_account_id, to_player_cents, kind),
        ],
        idempotency_key=idempotency_key,
        ref_type="pool",
        ref_id=pool_id,
    )


def pool_buyin(
    conn: Connection,
    clock: Clock,
    account_id: int,
    escrow_account_id: int,
    amount_cents: int,
    *,
    pool_id: int,
    idempotency_key: str,
) -> PostResult:
    _require_positive(amount_cents)
    return _pool_move(
        conn,
        clock,
        kind=EntryKind.POOL_BUYIN,
        escrow_account_id=escrow_account_id,
        player_account_id=account_id,
        to_player_cents=-amount_cents,
        idempotency_key=idempotency_key,
        pool_id=pool_id,
    )


def pool_payout(
    conn: Connection,
    clock: Clock,
    account_id: int,
    escrow_account_id: int,
    amount_cents: int,
    *,
    pool_id: int,
    idempotency_key: str,
) -> PostResult:
    _require_positive(amount_cents)
    return _pool_move(
        conn,
        clock,
        kind=EntryKind.POOL_PAYOUT,
        escrow_account_id=escrow_account_id,
        player_account_id=account_id,
        to_player_cents=amount_cents,
        idempotency_key=idempotency_key,
        pool_id=pool_id,
    )


def pool_refund(
    conn: Connection,
    clock: Clock,
    account_id: int,
    escrow_account_id: int,
    amount_cents: int,
    *,
    pool_id: int,
    idempotency_key: str,
) -> PostResult:
    _require_positive(amount_cents)
    return _pool_move(
        conn,
        clock,
        kind=EntryKind.POOL_REFUND,
        escrow_account_id=escrow_account_id,
        player_account_id=account_id,
        to_player_cents=amount_cents,
        idempotency_key=idempotency_key,
        pool_id=pool_id,
    )


# ---- verification -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AccountMismatch:
    account_id: int
    stored_balance_cents: int
    computed_balance_cents: int
    stored_pnl_cents: int
    computed_pnl_cents: int


@dataclass(frozen=True, slots=True)
class VerifyReport:
    accounts_checked: int
    txns_checked: int
    mismatches: list[AccountMismatch] = field(default_factory=list)
    unbalanced_txn_ids: list[int] = field(default_factory=list)
    negative_player_account_ids: list[int] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (self.mismatches or self.unbalanced_txn_ids or self.negative_player_account_ids)


def verify(conn: Connection) -> VerifyReport:
    """Recompute every cached balance and P&L from the entries and compare."""
    betting = [k.value for k in BETTING_KINDS]
    computed_balance = func.coalesce(func.sum(LedgerEntry.amount_cents), 0)
    computed_pnl = func.coalesce(
        func.sum(case((LedgerEntry.kind.in_(betting), LedgerEntry.amount_cents), else_=0)), 0
    )
    rows = conn.execute(
        select(
            Account.id,
            Account.kind,
            Account.balance_cents,
            Account.pnl_cents,
            computed_balance.label("balance"),
            computed_pnl.label("pnl"),
        )
        .outerjoin(LedgerEntry, LedgerEntry.account_id == Account.id)
        .group_by(Account.id)
        .order_by(Account.id)
    ).all()
    mismatches = [
        AccountMismatch(r.id, r.balance_cents, r.balance, r.pnl_cents, r.pnl)
        for r in rows
        if (r.balance_cents, r.pnl_cents) != (r.balance, r.pnl)
    ]
    negative = [r.id for r in rows if r.kind == AccountKind.PLAYER.value and r.balance < 0]

    txn_sums = (
        select(
            LedgerTxn.id,
            func.coalesce(func.sum(LedgerEntry.amount_cents), 0).label("total"),
            func.count(LedgerEntry.id).label("n"),
        )
        .outerjoin(LedgerEntry, LedgerEntry.txn_id == LedgerTxn.id)
        .group_by(LedgerTxn.id)
        .subquery()
    )
    txns_checked = conn.execute(select(func.count()).select_from(txn_sums)).scalar_one()
    unbalanced = list(
        conn.execute(
            select(txn_sums.c.id)
            .where((txn_sums.c.total != 0) | (txn_sums.c.n < 2))
            .order_by(txn_sums.c.id)
        ).scalars()
    )
    return VerifyReport(
        accounts_checked=len(rows),
        txns_checked=txns_checked,
        mismatches=mismatches,
        unbalanced_txn_ids=unbalanced,
        negative_player_account_ids=negative,
    )
