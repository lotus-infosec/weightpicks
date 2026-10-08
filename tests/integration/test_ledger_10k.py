"""Measure of success: `wp ledger verify` passes after 10,000 random operations.

Deterministic (seeded) mix of every primitive, including deliberate overdraw attempts
and idempotent replays, each in its own BEGIN IMMEDIATE transaction like production.
"""

import random
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from app.cli import main
from app.core.clock import SimClock
from app.core.db import immediate, make_engine
from app.domain.ledger import AccountKind, InsufficientFunds
from app.models import Account
from app.services import ledger
from app.services.users import ensure_player

OPERATIONS = 10_000
PLAYERS = 10
SEED = 20261012


def test_ledger_verify_passes_after_10k_random_operations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LOG_FORMAT", "console")
    assert main(["migrate"]) == 0

    rng = random.Random(SEED)
    clock = SimClock(datetime(2026, 10, 12, tzinfo=UTC))
    engine = make_engine(f"sqlite:///{tmp_path / 'app.db'}")
    with immediate(engine) as conn:
        season_id = ledger.open_season(conn, clock)
        accounts = [
            ledger.open_player_account(
                conn, clock, season_id, ensure_player(conn, clock, f"r{n}@example.invalid", "R")
            )
            for n in range(PLAYERS)
        ]
    balance = dict.fromkeys(accounts, 0)
    pnl = dict.fromkeys(accounts, 0)
    posted: list[tuple[str, int, int, str]] = []
    counts = {"ok": 0, "insufficient": 0, "replayed": 0}

    def post(kind: str, account: int, amount: int, key: str) -> bool:
        with immediate(engine) as conn:
            match kind:
                case "grant":
                    r = ledger.grant_starting(conn, clock, account, amount, idempotency_key=key)
                case "allowance":
                    r = ledger.pay_allowance(conn, clock, account, amount, idempotency_key=key)
                case "bailout":
                    r = ledger.bailout(conn, clock, account, amount, idempotency_key=key)
                case "adjust":
                    r = ledger.admin_adjust(
                        conn,
                        clock,
                        account,
                        amount,
                        reason="r",
                        created_by=None,
                        idempotency_key=key,
                    )
                case "stake":
                    r = ledger.stake(conn, clock, account, amount, idempotency_key=key)
                case "payout":
                    r = ledger.payout(conn, clock, account, amount, idempotency_key=key)
                case _:
                    r = ledger.refund(conn, clock, account, amount, idempotency_key=key)
        return r.replayed

    kinds = ["stake", "payout", "refund", "allowance", "bailout", "adjust", "grant", "replay"]
    weights = [40, 15, 8, 12, 4, 8, 3, 10]
    for n in range(OPERATIONS):
        kind = rng.choices(kinds, weights)[0]
        if kind == "replay" and posted:
            k, account, amount, key = rng.choice(posted)
            assert post(k, account, amount, key) is True
            counts["replayed"] += 1
            continue
        if kind == "replay":
            kind = "grant"
        account = rng.choice(accounts)
        if kind == "adjust":
            amount = rng.choice([-1, 1]) * rng.randint(1, 60_000)
        elif kind == "stake":
            # Mostly affordable stakes, sometimes a deliberate overdraw attempt.
            amount = rng.randint(1, max(1, balance[account] + rng.choice([0, 0, 0, 25_000])))
        else:
            amount = rng.randint(1, 60_000)
        key = f"op:{len(posted)}"
        overdraw = (
            kind in {"stake", "adjust"}
            and balance[account] + (-amount if kind == "stake" else amount) < 0
        )
        try:
            post(kind, account, amount, key)
        except InsufficientFunds:
            assert overdraw, f"op {n}: unexpected InsufficientFunds"
            counts["insufficient"] += 1
            continue
        assert not overdraw, f"op {n}: overdraw was accepted"
        posted.append((kind, account, amount, key))
        delta = -amount if kind == "stake" else amount
        balance[account] += delta
        if kind in {"stake", "payout", "refund"}:
            pnl[account] += delta
        counts["ok"] += 1

    with engine.connect() as conn:
        stored = {
            r.id: (r.balance_cents, r.pnl_cents)
            for r in conn.execute(
                select(Account.id, Account.balance_cents, Account.pnl_cents).where(
                    Account.kind == AccountKind.PLAYER.value
                )
            )
        }
    engine.dispose()

    assert sum(counts.values()) == OPERATIONS
    assert counts["insufficient"] > 0 and counts["replayed"] > 0
    assert stored == {a: (balance[a], pnl[a]) for a in accounts}
    capsys.readouterr()
    assert main(["ledger", "verify"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("ledger OK:")
    print(f"\n10k run: {counts} -> {out.strip()}")
