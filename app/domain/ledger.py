"""Ledger vocabulary and invariants (BUILD_PLAN §1.3, D-007). Pure: no I/O.

Every money movement is one transaction whose entries sum to zero. P&L counts
only `BETTING_KINDS`; grants, allowances, bailouts and admin adjustments move
balance but never P&L.
"""

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum


class AccountKind(StrEnum):
    PLAYER = "player"
    MINT = "mint"
    HOUSE = "house"
    POOL = "pool"


class EntryKind(StrEnum):
    STARTING_GRANT = "starting_grant"
    ALLOWANCE = "allowance"
    BAILOUT = "bailout"
    ADMIN_ADJUST = "admin_adjust"
    BET_STAKE = "bet_stake"
    BET_PAYOUT = "bet_payout"
    BET_REFUND = "bet_refund"
    POOL_BUYIN = "pool_buyin"
    POOL_PAYOUT = "pool_payout"
    POOL_REFUND = "pool_refund"


# The single definition of what counts toward P&L.
BETTING_KINDS: frozenset[EntryKind] = frozenset(
    {
        EntryKind.BET_STAKE,
        EntryKind.BET_PAYOUT,
        EntryKind.BET_REFUND,
        EntryKind.POOL_BUYIN,
        EntryKind.POOL_PAYOUT,
        EntryKind.POOL_REFUND,
    }
)


class LedgerError(Exception):
    """Base class for every ledger rule violation."""


class UnbalancedTxn(LedgerError):
    pass


class InsufficientFunds(LedgerError):
    pass


class IdempotencyConflict(LedgerError):
    pass


class AccountNotFound(LedgerError):
    pass


class CrossSeasonTxn(LedgerError):
    pass


@dataclass(frozen=True, slots=True)
class Entry:
    account_id: int
    amount_cents: int
    kind: EntryKind

    def __post_init__(self) -> None:
        if type(self.amount_cents) is not int or type(self.account_id) is not int:
            raise TypeError("ledger entries need int account ids and int cents")


def validate_entries(entries: Sequence[Entry]) -> None:
    """Raise `UnbalancedTxn` unless the entries form a valid double-entry transaction."""
    if len(entries) < 2:
        raise UnbalancedTxn("a transaction needs at least two entries")
    if len({e.account_id for e in entries}) < 2:
        raise UnbalancedTxn("a transaction must touch at least two accounts")
    if any(e.amount_cents == 0 for e in entries):
        raise UnbalancedTxn("entries must not be zero")
    total = sum(e.amount_cents for e in entries)
    if total != 0:
        raise UnbalancedTxn(f"entries must sum to zero, got {total}")


def fingerprint(
    kind: EntryKind,
    entries: Sequence[Entry],
    *,
    ref_type: str | None,
    ref_id: int | None,
    memo: str | None,
    created_by: int | None,
) -> str:
    """Stable hash of a transaction's content, used to tell a replay from a conflict."""
    canonical = {
        "kind": kind.value,
        "entries": sorted([e.account_id, e.amount_cents, e.kind.value] for e in entries),
        "ref_type": ref_type,
        "ref_id": ref_id,
        "memo": memo,
        "created_by": created_by,
    }
    payload = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()
