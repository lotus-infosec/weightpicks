"""STAGE16 chaos run: 30 simulated days of the real worker jobs while the worker is killed
at random (before a job's work, or after it but before its claim is marked done), restarted
at random, and one Garmin sync in five fails. Bettors keep betting through it.

Invariants: the ledger verifies; every market settles at most once and only after a
successful sync past its settle time (never on stale data); every day still gets its
daily drop and every player their allowance exactly once; no claim is left `running`;
every settled bet has exactly one result post.
"""

from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

import numpy as np
import pytest
from sqlalchemy import func, select

from app.core.clock import SimClock
from app.models import (
    Bet,
    BetLeg,
    JobRun,
    LedgerEntry,
    LedgerTxn,
    Market,
    OutboxMessage,
    Settlement,
    SyncRun,
)
from app.providers.simulated import SimulatedProvider
from app.services import instance, ledger, sim
from app.services.bets import BetRejected, place_bet
from app.worker import registry
from app.worker.jobs import domain_jobs
from tests.season.harness import NY, _open_selections, start_season

pytestmark = pytest.mark.season

DAYS = 30
KILL_RATE = 0.004  # per job run: about one kill every other simulated day
RESTART_RATE = 0.002  # per tick: a clean restart every day or so
SYNC_FAILURE_RATE = 0.2


class Killed(BaseException):
    """The worker process dies: escapes `except Exception` like a real crash would."""


@dataclass
class Chaos:
    engine: Any
    start: datetime
    users: list[int]
    kills: Counter[str] = field(default_factory=Counter)
    restarts: int = 0
    released: int = 0
    sync_failures: int = 0
    bets: int = 0


@pytest.fixture(scope="module")
def chaos(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Chaos]:
    engine, settings, start, users, _admin = start_season(
        tmp_path_factory.mktemp("chaos"), players=6
    )
    rng = np.random.default_rng(16)
    run = Chaos(engine, start, users)
    calm = False  # the last days: no more failures, so everything can catch up
    patch = pytest.MonkeyPatch()
    fetch = SimulatedProvider.fetch_since

    def flaky_fetch(self: SimulatedProvider, since: datetime, now: datetime) -> Any:
        if not calm and rng.random() < SYNC_FAILURE_RATE:
            run.sync_failures += 1
            raise ConnectionError("chaos: Garmin is down")
        return fetch(self, since, now)

    finish = registry._finish

    def dying_finish(*args: Any, **kwargs: Any) -> None:
        if not calm and rng.random() < KILL_RATE:  # work committed; the claim stays `running`
            run.kills["after_work"] += 1
            raise Killed
        finish(*args, **kwargs)

    patch.setattr(SimulatedProvider, "fetch_since", flaky_fetch)
    patch.setattr(registry, "_finish", dying_finish)

    def boot(clock: SimClock) -> tuple[list[Any], dict[str, str]]:
        """A fresh worker process: recover dead claims, new job objects, empty memory."""
        run.released += len(registry.recover_interrupted(engine))
        jobs = list(domain_jobs(settings, instance.load(engine, clock, settings)))
        for job in jobs:
            original = job.run

            def dying_run(ctx: Any, _run: Any = original, _name: str = job.name) -> Any:
                if not calm and ctx.period_key != "tick" and rng.random() < KILL_RATE:
                    run.kills["before_work"] += 1
                    raise Killed
                return _run(ctx)

            job.run = dying_run  # type: ignore[method-assign]
        return jobs, {}

    clock = SimClock(start)
    jobs, seen = boot(clock)
    end = start + timedelta(days=DAYS)
    bet_times = {time(12, 0), time(19, 0)}
    while clock.now() < end:
        clock.set(clock.now() + sim.TICK)
        if rng.random() < RESTART_RATE:
            run.restarts += 1
            jobs, seen = boot(clock)
        try:
            registry.run_due(jobs, engine, clock, seen)
        except Killed:
            run.restarts += 1
            jobs, seen = boot(clock)
        local = clock.now().astimezone(NY)
        if local.time().replace(second=0, microsecond=0) in bet_times:
            options = _open_selections(engine, clock.now())
            for user in users:
                if not options or rng.random() > 0.7:
                    continue
                sel, ver, _ = options[int(rng.integers(len(options)))]
                try:
                    place_bet(
                        engine,
                        clock,
                        user_id=user,
                        selection_id=sel,
                        odds_version_id=ver,
                        stake_cents=int(rng.integers(100, 2_000)),
                        client_key=f"chaos-{local:%Y%m%d%H}-{user}",
                    )
                    run.bets += 1
                except BetRejected:
                    pass
    calm = True
    jobs, seen = boot(clock)  # a last restart: nothing may stay stuck
    for _ in range(3 * 24 * 60):  # three calm days so stale markets catch up
        clock.set(clock.now() + sim.TICK)
        registry.run_due(jobs, engine, clock, seen)
    patch.undo()
    yield run
    engine.dispose()


def test_chaos_actually_happened(chaos: Chaos) -> None:
    print(
        f"\nchaos: {DAYS} days, kills {dict(chaos.kills)}, restarts {chaos.restarts}, "
        f"claims released {chaos.released}, sync failures {chaos.sync_failures}, "
        f"bets {chaos.bets}"
    )
    assert sum(chaos.kills.values()) >= 5 and chaos.kills["after_work"] >= 1
    assert chaos.sync_failures >= 20 and chaos.bets >= 100 and chaos.released >= 1


def test_ledger_verifies(chaos: Chaos) -> None:
    with chaos.engine.connect() as conn:
        report = ledger.verify(conn)
    print(f"ledger: {report.accounts_checked} accounts, {report.txns_checked} txns")
    assert report.ok


def test_settlements_once_and_never_on_stale_data(chaos: Chaos) -> None:
    with chaos.engine.connect() as conn:
        settled = conn.execute(
            select(Market.id, Market.settle_after, Settlement.created_at).join(
                Settlement, Settlement.market_id == Market.id
            )
        ).all()
        per_market = Counter(
            conn.execute(select(Settlement.market_id)).scalars().all()
        )  # unique in the schema too
        syncs = (
            conn.execute(
                select(SyncRun.finished_at)
                .where(SyncRun.status == "ok")
                .order_by(SyncRun.finished_at)
            )
            .scalars()
            .all()
        )
        open_bets_on_settled = conn.execute(
            select(func.count())
            .select_from(Bet)
            .join(BetLeg, BetLeg.bet_id == Bet.id)
            .join(Market, Market.id == BetLeg.market_id)
            .where(Market.status == "settled", Bet.status == "open", Bet.kind == "single")
        ).scalar_one()
    assert settled and max(per_market.values()) == 1
    for market_id, settle_after, settled_at in settled:
        fresh = [f for f in syncs if settle_after < f <= settled_at]
        assert fresh, f"market {market_id} settled without a sync after {settle_after}"
    assert open_bets_on_settled == 0


def test_every_day_dropped_and_every_allowance_paid_once(chaos: Chaos) -> None:
    first = chaos.start.astimezone(NY).date()
    days = [first + timedelta(days=n) for n in range(1, DAYS)]
    with chaos.engine.connect() as conn:
        dropped = {
            d
            for (d,) in conn.execute(
                select(Market.window_start).where(Market.timeframe == "daily")
            ).all()
        }
        keys = Counter(
            conn.execute(
                select(LedgerTxn.idempotency_key).where(
                    LedgerTxn.idempotency_key.like("allowance:%")
                )
            )
            .scalars()
            .all()
        )
        allowance_entries = conn.execute(
            select(func.count()).select_from(LedgerEntry).where(LedgerEntry.kind == "allowance")
        ).scalar_one()
    missing = [d for d in days if _as_date(d) not in {_as_date(x) for x in dropped}]
    assert not missing, f"no daily drop on {missing}"
    assert keys and max(keys.values()) == 1
    assert allowance_entries >= len(chaos.users) * (DAYS - 2)


def test_no_claim_left_running_and_one_post_per_result(chaos: Chaos) -> None:
    with chaos.engine.connect() as conn:
        running = conn.execute(
            select(JobRun.job, JobRun.period_key).where(JobRun.status == "running")
        ).all()
        posts = Counter(
            conn.execute(
                select(OutboxMessage.dedupe_key).where(
                    OutboxMessage.dedupe_key.like("bet_result:%")
                )
            )
            .scalars()
            .all()
        )
        resolved = conn.execute(
            select(func.count()).select_from(Bet).where(Bet.status != "open")
        ).scalar_one()
    assert running == []
    assert max(posts.values()) == 1 and len(posts) == resolved


def _as_date(value: Any) -> date:
    return value.date() if isinstance(value, datetime) else value
