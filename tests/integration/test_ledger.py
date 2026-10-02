from dataclasses import dataclass

import pytest
from sqlalchemy import Engine, func, select, text

from app.core.clock import SimClock
from app.core.db import immediate
from app.domain.ledger import (
    AccountKind,
    AccountNotFound,
    CrossSeasonTxn,
    Entry,
    EntryKind,
    IdempotencyConflict,
    InsufficientFunds,
    UnbalancedTxn,
)
from app.domain.money import payout_cents
from app.models import Account, LedgerEntry, LedgerTxn
from app.services import ledger
from app.services.users import ensure_player


@dataclass(frozen=True)
class World:
    engine: Engine
    clock: SimClock
    season_id: int
    alice: int  # player account ids
    bob: int
    mint: int
    house: int


@pytest.fixture
def world(migrated_engine: Engine, clock: SimClock) -> World:
    with immediate(migrated_engine) as conn:
        season_id = ledger.open_season(conn, clock)
        alice_user = ensure_player(conn, clock, "Alice@Example.invalid", "Alice")
        bob_user = ensure_player(conn, clock, "bob@example.invalid", "Bob")
        alice = ledger.open_player_account(conn, clock, season_id, alice_user)
        bob = ledger.open_player_account(conn, clock, season_id, bob_user)
        mint = ledger.system_account(conn, season_id, AccountKind.MINT)
        house = ledger.system_account(conn, season_id, AccountKind.HOUSE)
        ledger.grant_starting(conn, clock, alice, 100_000, idempotency_key="grant:alice")
        ledger.grant_starting(conn, clock, bob, 100_000, idempotency_key="grant:bob")
    return World(migrated_engine, clock, season_id, alice, bob, mint, house)


def _account(engine: Engine, account_id: int) -> tuple[int, int]:
    with engine.connect() as conn:
        row = conn.execute(
            select(Account.balance_cents, Account.pnl_cents).where(Account.id == account_id)
        ).one()
    return row.balance_cents, row.pnl_cents


def _counts(engine: Engine) -> tuple[int, int]:
    with engine.connect() as conn:
        txns = conn.execute(select(func.count()).select_from(LedgerTxn)).scalar_one()
        entries = conn.execute(select(func.count()).select_from(LedgerEntry)).scalar_one()
    return txns, entries


# ---- the worked example from BUILD_PLAN §1.3 ---------------------------------


def test_win_at_minus_110_gives_pnl_plus_9090(world: World) -> None:
    with immediate(world.engine) as conn:
        ledger.stake(conn, world.clock, world.alice, 10_000, idempotency_key="stake:1", ref_id=1)
        ledger.payout(
            conn,
            world.clock,
            world.alice,
            payout_cents(10_000, -110),
            idempotency_key="payout:1",
            ref_id=1,
        )
    assert _account(world.engine, world.alice) == (100_000 + 9_090, 9_090)
    assert _account(world.engine, world.house) == (-9_090, -9_090)


def test_push_gives_pnl_zero(world: World) -> None:
    with immediate(world.engine) as conn:
        ledger.stake(conn, world.clock, world.alice, 10_000, idempotency_key="stake:2")
        ledger.refund(conn, world.clock, world.alice, 10_000, idempotency_key="refund:2")
    assert _account(world.engine, world.alice) == (100_000, 0)


def test_loss_is_negative_pnl(world: World) -> None:
    with immediate(world.engine) as conn:
        ledger.stake(conn, world.clock, world.bob, 2_500, idempotency_key="stake:3")
    assert _account(world.engine, world.bob) == (97_500, -2_500)


# ---- non-betting money never touches P&L --------------------------------------


def test_grant_allowance_bailout_and_adjust_never_change_pnl(world: World) -> None:
    with immediate(world.engine) as conn:
        ledger.pay_allowance(conn, world.clock, world.alice, 5_000, idempotency_key="allow:1")
        ledger.bailout(conn, world.clock, world.alice, 50_000, idempotency_key="bail:1")
        ledger.admin_adjust(
            conn,
            world.clock,
            world.alice,
            -1_000,
            reason="duplicate allowance",
            created_by=None,
            idempotency_key="adj:1",
        )
    assert _account(world.engine, world.alice) == (100_000 + 5_000 + 50_000 - 1_000, 0)
    assert _account(world.engine, world.mint)[1] == 0


# ---- idempotency ----------------------------------------------------------------


def test_exact_replay_is_a_noop_returning_the_original(world: World) -> None:
    with immediate(world.engine) as conn:
        first = ledger.stake(conn, world.clock, world.alice, 1_000, idempotency_key="stake:r")
    before = _counts(world.engine)
    with immediate(world.engine) as conn:
        again = ledger.stake(conn, world.clock, world.alice, 1_000, idempotency_key="stake:r")
    assert again.txn_id == first.txn_id
    assert (first.replayed, again.replayed) == (False, True)
    assert _counts(world.engine) == before
    assert _account(world.engine, world.alice) == (99_000, -1_000)


def test_replay_with_different_content_raises(world: World) -> None:
    with immediate(world.engine) as conn:
        ledger.stake(conn, world.clock, world.alice, 1_000, idempotency_key="stake:c")
    with pytest.raises(IdempotencyConflict), immediate(world.engine) as conn:
        ledger.stake(conn, world.clock, world.alice, 2_000, idempotency_key="stake:c")


# ---- guards -----------------------------------------------------------------------


def test_insufficient_funds_writes_nothing(world: World) -> None:
    before = _counts(world.engine)
    with pytest.raises(InsufficientFunds), immediate(world.engine) as conn:
        ledger.stake(conn, world.clock, world.alice, 100_001, idempotency_key="stake:big")
    assert _counts(world.engine) == before
    assert _account(world.engine, world.alice) == (100_000, 0)


def test_staking_the_whole_balance_is_allowed(world: World) -> None:
    with immediate(world.engine) as conn:
        ledger.stake(conn, world.clock, world.alice, 100_000, idempotency_key="stake:all")
    assert _account(world.engine, world.alice) == (0, -100_000)


def test_negative_admin_adjust_cannot_overdraw(world: World) -> None:
    with pytest.raises(InsufficientFunds), immediate(world.engine) as conn:
        ledger.admin_adjust(
            conn,
            world.clock,
            world.alice,
            -100_001,
            reason="claw back",
            created_by=None,
            idempotency_key="adj:over",
        )


@pytest.mark.parametrize("reason", ["", "   "])
def test_admin_adjust_requires_a_reason(world: World, reason: str) -> None:
    with pytest.raises(ValueError, match="reason"), immediate(world.engine) as conn:
        ledger.admin_adjust(
            conn,
            world.clock,
            world.alice,
            500,
            reason=reason,
            created_by=None,
            idempotency_key="adj:noreason",
        )


@pytest.mark.parametrize("amount", [0, -1])
def test_primitives_require_positive_amounts(world: World, amount: int) -> None:
    with pytest.raises(ValueError, match="positive"), immediate(world.engine) as conn:
        ledger.payout(conn, world.clock, world.alice, amount, idempotency_key="p:bad")


def test_admin_adjust_rejects_zero(world: World) -> None:
    with pytest.raises(ValueError, match="zero"), immediate(world.engine) as conn:
        ledger.admin_adjust(
            conn, world.clock, world.alice, 0, reason="x", created_by=None, idempotency_key="z"
        )


def test_unbalanced_post_raises(world: World) -> None:
    with pytest.raises(UnbalancedTxn), immediate(world.engine) as conn:
        ledger.post_txn(
            conn,
            world.clock,
            kind=EntryKind.ALLOWANCE,
            entries=[
                Entry(world.mint, -100, EntryKind.ALLOWANCE),
                Entry(world.alice, 99, EntryKind.ALLOWANCE),
            ],
            idempotency_key="unbalanced",
        )


def test_unknown_account_raises(world: World) -> None:
    with pytest.raises(AccountNotFound), immediate(world.engine) as conn:
        ledger.stake(conn, world.clock, 9_999, 100, idempotency_key="ghost")


def test_money_cannot_cross_seasons(world: World) -> None:
    with immediate(world.engine) as conn:
        conn.execute(text("UPDATE seasons SET ended_at = '2026-10-06 00:00:00.000000'"))
        new_season = ledger.open_season(conn, world.clock)
        new_mint = ledger.system_account(conn, new_season, AccountKind.MINT)
    with pytest.raises(CrossSeasonTxn), immediate(world.engine) as conn:
        ledger.post_txn(
            conn,
            world.clock,
            kind=EntryKind.ALLOWANCE,
            entries=[
                Entry(new_mint, -100, EntryKind.ALLOWANCE),
                Entry(world.alice, 100, EntryKind.ALLOWANCE),
            ],
            idempotency_key="cross",
        )


def test_open_player_account_is_idempotent(world: World) -> None:
    with immediate(world.engine) as conn:
        user = ensure_player(conn, world.clock, "ALICE@example.invalid", "Alice again")
        assert ledger.open_player_account(conn, world.clock, world.season_id, user) == world.alice


def test_txn_records_metadata(world: World) -> None:
    with immediate(world.engine) as conn:
        result = ledger.stake(
            conn, world.clock, world.alice, 700, idempotency_key="stake:meta", ref_id=42
        )
    with world.engine.connect() as conn:
        txn = conn.execute(select(LedgerTxn).where(LedgerTxn.id == result.txn_id)).one()
    assert (txn.kind, txn.ref_type, txn.ref_id) == ("bet_stake", "bet", 42)
    assert txn.created_at.replace(tzinfo=None) == world.clock.now().replace(tzinfo=None)


# ---- verify -------------------------------------------------------------------------


def test_verify_is_clean_after_normal_activity(world: World) -> None:
    with immediate(world.engine) as conn:
        ledger.stake(conn, world.clock, world.alice, 10_000, idempotency_key="s")
        ledger.payout(conn, world.clock, world.alice, 19_090, idempotency_key="p")
        report = ledger.verify(conn)
    assert report.ok
    assert report.accounts_checked == 4


def test_verify_detects_a_tampered_cache(world: World) -> None:
    with immediate(world.engine) as conn:
        conn.execute(text("UPDATE accounts SET pnl_cents = 1 WHERE id = :id"), {"id": world.bob})
        report = ledger.verify(conn)
    assert not report.ok
    assert [m.account_id for m in report.mismatches] == [world.bob]
    assert report.mismatches[0].stored_pnl_cents == 1
    assert report.mismatches[0].computed_pnl_cents == 0
