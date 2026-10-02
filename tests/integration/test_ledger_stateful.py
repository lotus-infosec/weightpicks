"""Hypothesis stateful test: random ledger operations against an in-test model.

After every step: Σ entries = 0, cached balance/P&L equal the model, verify() is
clean and no player is below zero. Each example starts from a copy of one
pre-migrated template database so examples stay fast and isolated.
"""

import atexit
import shutil
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from functools import cache
from pathlib import Path

from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule
from sqlalchemy import Connection, func, select

from app.core.clock import SimClock
from app.core.db import immediate, make_engine
from app.core.migrations import upgrade_to_head
from app.domain.ledger import AccountKind, InsufficientFunds
from app.models import Account, LedgerEntry
from app.services import ledger
from app.services.ledger import PostResult
from app.services.users import ensure_player

PLAYERS = 3
START = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)
players = st.integers(min_value=0, max_value=PLAYERS - 1)
amounts = st.integers(min_value=1, max_value=300_000)


@cache
def _template() -> Path:
    directory = Path(tempfile.mkdtemp(prefix="wp-ledger-template-"))
    atexit.register(shutil.rmtree, directory, True)
    engine = make_engine(f"sqlite:///{directory / 'app.db'}")
    upgrade_to_head(engine, directory / ".migrate.lock")
    clock = SimClock(START)
    with immediate(engine) as conn:
        season_id = ledger.open_season(conn, clock)
        for n in range(PLAYERS):
            user_id = ensure_player(conn, clock, f"p{n}@example.invalid", f"P{n}")
            ledger.open_player_account(conn, clock, season_id, user_id)
    engine.dispose()  # checkpoints the WAL so the single file is complete
    return directory / "app.db"


Op = Callable[[Connection, str], PostResult]


class LedgerMachine(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self.directory = Path(tempfile.mkdtemp(prefix="wp-ledger-"))
        shutil.copy(_template(), self.directory / "app.db")
        self.engine = make_engine(f"sqlite:///{self.directory / 'app.db'}")
        self.clock = SimClock(START)
        with self.engine.connect() as conn:
            rows = conn.execute(select(Account.id, Account.kind).order_by(Account.id)).all()
        self.player_ids = [r.id for r in rows if r.kind == AccountKind.PLAYER.value]
        self.mint = next(r.id for r in rows if r.kind == AccountKind.MINT.value)
        self.house = next(r.id for r in rows if r.kind == AccountKind.HOUSE.value)
        self.balance = {r.id: 0 for r in rows}
        self.pnl = {r.id: 0 for r in rows}
        self.counter = 0
        self.last: tuple[Op, str] | None = None

    def teardown(self) -> None:
        self.engine.dispose()
        shutil.rmtree(self.directory, ignore_errors=True)

    # ---- helpers ----------------------------------------------------------------

    def _post(self, op: Op, *, fails: bool) -> bool:
        self.counter += 1
        key = f"op:{self.counter}"
        try:
            with immediate(self.engine) as conn:
                result = op(conn, key)
        except InsufficientFunds:
            assert fails, "unexpected InsufficientFunds"
            return False
        assert not fails, "expected InsufficientFunds"
        assert not result.replayed
        self.last = (op, key)
        return True

    def _move(self, player: int, source: int, amount: int, *, betting: bool) -> None:
        self.balance[player] += amount
        self.balance[source] -= amount
        if betting:
            self.pnl[player] += amount
            self.pnl[source] -= amount

    # ---- rules: mint -> player (never P&L) -------------------------------------------

    @rule(i=players, amount=amounts, kind=st.sampled_from(["grant", "allowance", "bailout"]))
    def credit(self, i: int, amount: int, kind: str) -> None:
        pid = self.player_ids[i]
        fn = {
            "grant": ledger.grant_starting,
            "allowance": ledger.pay_allowance,
            "bailout": ledger.bailout,
        }[kind]
        self._post(
            lambda c, k: fn(c, self.clock, pid, amount, idempotency_key=k),
            fails=False,
        )
        self._move(pid, self.mint, amount, betting=False)

    @rule(i=players, amount=st.integers(min_value=-300_000, max_value=300_000).filter(bool))
    def adjust(self, i: int, amount: int) -> None:
        pid = self.player_ids[i]
        fails = self.balance[pid] + amount < 0
        ok = self._post(
            lambda c, k: ledger.admin_adjust(
                c, self.clock, pid, amount, reason="test", created_by=None, idempotency_key=k
            ),
            fails=fails,
        )
        if ok:
            self._move(pid, self.mint, amount, betting=False)

    # ---- rules: betting (player <-> house, counts toward P&L) --------------------------

    @rule(i=players, amount=amounts)
    def stake(self, i: int, amount: int) -> None:
        pid = self.player_ids[i]
        ok = self._post(
            lambda c, k: ledger.stake(c, self.clock, pid, amount, idempotency_key=k),
            fails=amount > self.balance[pid],
        )
        if ok:
            self._move(pid, self.house, -amount, betting=True)

    @rule(i=players, amount=amounts, kind=st.sampled_from(["payout", "refund"]))
    def pay(self, i: int, amount: int, kind: str) -> None:
        pid = self.player_ids[i]
        fn = ledger.payout if kind == "payout" else ledger.refund
        self._post(lambda c, k: fn(c, self.clock, pid, amount, idempotency_key=k), fails=False)
        self._move(pid, self.house, amount, betting=True)

    # ---- rules: idempotent replay ---------------------------------------------------------

    @precondition(lambda self: self.last is not None)
    @rule()
    def replay_last(self) -> None:
        assert self.last is not None
        op, key = self.last
        with immediate(self.engine) as conn:
            assert op(conn, key).replayed  # model unchanged

    # ---- invariants ---------------------------------------------------------------------

    @invariant()
    def ledger_matches_model(self) -> None:
        with self.engine.connect() as conn:
            report = ledger.verify(conn)
            stored = {
                r.id: (r.balance_cents, r.pnl_cents)
                for r in conn.execute(select(Account.id, Account.balance_cents, Account.pnl_cents))
            }
            total = conn.execute(
                select(func.coalesce(func.sum(LedgerEntry.amount_cents), 0))
            ).scalar_one()
        assert report.ok, report
        assert total == 0
        assert stored == {aid: (self.balance[aid], self.pnl[aid]) for aid in stored}
        assert all(self.balance[pid] >= 0 for pid in self.player_ids)


LedgerMachine.TestCase.settings = settings(max_examples=40, stateful_step_count=40, deadline=None)
TestLedgerStateMachine = LedgerMachine.TestCase
