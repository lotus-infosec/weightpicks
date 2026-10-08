import pytest

from app.domain.economy import DEFAULT_ECONOMY
from app.domain.ledger import (
    BETTING_KINDS,
    Entry,
    EntryKind,
    UnbalancedTxn,
    fingerprint,
    validate_entries,
)


def test_betting_kinds_are_exactly_the_six_from_the_plan() -> None:
    assert {k.value for k in BETTING_KINDS} == {
        "bet_stake",
        "bet_payout",
        "bet_refund",
        "pool_buyin",
        "pool_payout",
        "pool_refund",
    }
    non_betting = set(EntryKind) - BETTING_KINDS
    assert {k.value for k in non_betting} == {
        "starting_grant",
        "allowance",
        "bailout",
        "admin_adjust",
        "season_carry",  # D-043: carried balances never count as profit
    }


def test_balanced_entries_pass() -> None:
    validate_entries([Entry(1, -500, EntryKind.BET_STAKE), Entry(2, 500, EntryKind.BET_STAKE)])
    validate_entries(
        [
            Entry(1, -300, EntryKind.POOL_PAYOUT),
            Entry(2, 200, EntryKind.POOL_PAYOUT),
            Entry(3, 100, EntryKind.POOL_PAYOUT),
        ]
    )


@pytest.mark.parametrize(
    ("entries", "message"),
    [
        ([Entry(1, -500, EntryKind.BET_STAKE), Entry(2, 499, EntryKind.BET_STAKE)], "sum"),
        ([Entry(1, 0, EntryKind.ALLOWANCE), Entry(2, 0, EntryKind.ALLOWANCE)], "zero"),
        ([Entry(1, -5, EntryKind.ALLOWANCE)], "at least two"),
        ([Entry(1, -5, EntryKind.ALLOWANCE), Entry(1, 5, EntryKind.ALLOWANCE)], "two accounts"),
        ([], "at least two"),
    ],
)
def test_invalid_entries_raise(entries: list[Entry], message: str) -> None:
    with pytest.raises(UnbalancedTxn, match=message):
        validate_entries(entries)


def test_entry_rejects_float_amounts() -> None:
    with pytest.raises(TypeError):
        Entry(1, 5.0, EntryKind.ALLOWANCE)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        Entry(1, True, EntryKind.ALLOWANCE)


def test_fingerprint_is_order_independent_and_content_sensitive() -> None:
    a = [Entry(1, -500, EntryKind.BET_STAKE), Entry(2, 500, EntryKind.BET_STAKE)]
    base = fingerprint(EntryKind.BET_STAKE, a, ref_type="bet", ref_id=7, memo=None, created_by=None)
    reordered = fingerprint(
        EntryKind.BET_STAKE, a[::-1], ref_type="bet", ref_id=7, memo=None, created_by=None
    )
    assert base == reordered
    other_amount = [Entry(1, -501, EntryKind.BET_STAKE), Entry(2, 501, EntryKind.BET_STAKE)]
    assert base != fingerprint(
        EntryKind.BET_STAKE, other_amount, ref_type="bet", ref_id=7, memo=None, created_by=None
    )
    assert base != fingerprint(
        EntryKind.BET_STAKE, a, ref_type="bet", ref_id=8, memo=None, created_by=None
    )


def test_economy_defaults_match_concept() -> None:
    # The default economy; configurable in /setup.
    assert DEFAULT_ECONOMY.starting_bankroll_cents == 100_000
    assert DEFAULT_ECONOMY.daily_allowance_cents == 5_000
    assert DEFAULT_ECONOMY.bailout_cents == 50_000
    assert DEFAULT_ECONOMY.bailout_cooldown_days == 2
