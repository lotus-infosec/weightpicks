"""Goal Reached, freeze and new seasons (BUILD_PLAN §1.4.2, D-011, D-043).

Goal Reached runs once per season (job key `goal_reached` / `<season_id>`, shared by the
automatic and the manual trigger): freeze first so no bet slips in, settle what the data
already decides, void and refund everything else, finish the pools, announce it. A new
season starts only from the frozen state: it ends the old season, opens fresh accounts
with each player's balance carried over (Q3 -> carry, outside P&L) and unfreezes.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import Connection, Engine, insert, select, update
from sqlalchemy.exc import IntegrityError

from app.core.clock import Clock
from app.core.db import immediate
from app.domain.ledger import AccountKind
from app.domain.markets import MarketStatus
from app.domain.schedule import local_date
from app.models import Account, AiProposal, JobRun, Market, Pool, Season, User
from app.services import admin, audit, busts, instance, leaderboard, ledger, pools, settlement
from app.services.observations import canonical_weigh_ins, latest_complete_through
from app.services.outbox import Category, enqueue

log = structlog.get_logger()

GOAL_JOB = "goal_reached"
VOID_REASON = "Goal reached"


class SeasonError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def reached(direction: str | None, value_x10: int, goal_x10: int) -> bool:
    return value_x10 >= goal_x10 if direction == "up" else value_x10 <= goal_x10


@dataclass(frozen=True, slots=True)
class Hit:
    season_id: int
    observation_id: int
    value_x10: int
    day: str


def _season(conn: Connection) -> Any:
    """The current (not ended) season row, or None."""
    return conn.execute(select(Season).where(Season.ended_at.is_(None))).one_or_none()


@dataclass(frozen=True, slots=True)
class Scan:
    """One goal check: the season looked at, the last complete day scanned, any hit."""

    season_id: int | None
    complete: date | None
    hit: Hit | None = None
    watching: bool = True  # False: no active season with a goal, nothing to watch


def scan(conn: Connection, *, after: tuple[int | None, date | None] = (None, None)) -> Scan:
    """Look for the first canonical weigh-in this season that reaches the goal, among
    days whose data is complete (a successful sync after that day's weigh-in window).
    `after` is the previous scan's (season, day): days up to it are skipped, since a
    complete day's canonical weigh-in can't change. No goal or already reached: no hit."""
    season = _season(conn)
    if season is None or season.status != "active" or season.goal_weight_x10 is None:
        return Scan(season.id if season is not None else None, None, watching=False)
    complete = latest_complete_through(conn).get("weight")
    done = after[1] if after[0] == season.id else None
    if complete is None or (done is not None and complete <= done):
        return Scan(season.id, complete or done)
    config = instance.read(conn)
    if config is None:
        return Scan(season.id, None)
    first = local_date(season.started_at, config.tz)
    if done is not None:
        first = max(first, done + timedelta(days=1))
    for c in canonical_weigh_ins(conn, first, complete):
        if reached(season.direction, c.value, season.goal_weight_x10):
            return Scan(
                season.id,
                complete,
                Hit(season.id, c.observation_id, c.value, c.local_date.isoformat()),
            )
    return Scan(season.id, complete)


def detect(conn: Connection) -> Hit | None:
    """The goal-reaching weigh-in this season, if any (a full scan)."""
    return scan(conn).hit


@dataclass(frozen=True, slots=True)
class GoalResult:
    season_id: int
    settled: int
    voided_markets: int
    pools_settled: int
    pools_refunded: int


def goal_reached(
    engine: Engine,
    clock: Clock,
    *,
    hit: Hit | None = None,
    actor: audit.Actor | None = None,
) -> GoalResult | None:
    """Run Goal Reached for the current season (D-011). None if it already ran."""
    with immediate(engine) as conn:
        season = _season(conn)
        if season is None:
            raise SeasonError("no_season", "No season is running.")
        now = clock.now()
        try:
            conn.execute(
                insert(JobRun).values(
                    job=GOAL_JOB,
                    period_key=str(season.id),
                    status="ok",
                    started_at=now,
                    finished_at=now,
                )
            )
        except IntegrityError:
            return None
        if hit is None and actor is not None:  # manual: use today's weigh-in if it counts
            hit = detect(conn)
        # 1. Freeze first (locks every open market) so nothing can slip in.
        instance.set_instance_state(conn, clock, instance.FROZEN)
        conn.execute(
            update(Season)
            .where(Season.id == season.id)
            .values(
                status="frozen",
                goal_reached_at=now,
                goal_observation_id=hit.observation_id if hit else None,
            )
        )
        audit.record(
            conn,
            clock,
            actor,
            action="season.goal_reached",
            target=("season", season.id),
            after={
                "trigger": "manual" if actor else "auto",
                "value_x10": hit.value_x10 if hit else None,
                "day": hit.day if hit else None,
            },
        )
        season_id, number = season.id, season.number
        start_x10, goal_x10, started = (
            season.start_weight_x10,
            season.goal_weight_x10,
            season.started_at,
        )
    # 2-3. Settle what complete data decides (incl. milestones the goal weigh-in crossed).
    settled = settlement.settle_due(engine, clock).settled
    # 4. Void and refund everything that can't be decided yet.
    with immediate(engine) as conn:
        left = (
            conn.execute(
                select(Market.id)
                .where(
                    Market.season_id == season_id,
                    Market.status.in_((MarketStatus.OPEN.value, MarketStatus.LOCKED.value)),
                )
                .order_by(Market.id)
            )
            .scalars()
            .all()
        )
        for market_id in left:
            admin.void_open_market(conn, clock, market_id, VOID_REASON)
    # 5. Pools: settle the ones whose target day is complete, refund the rest.
    with engine.connect() as conn:
        open_pools = (
            conn.execute(
                select(Pool.id).where(
                    Pool.season_id == season_id, Pool.status.in_(("open", "locked"))
                )
            )
            .scalars()
            .all()
        )
    finished = refunded = 0
    for pool_id in open_pools:
        if pools.settle_pool(engine, clock, pool_id):
            finished += 1
            continue
        with immediate(engine) as conn:
            refunded += pools.refund_pool(conn, clock, pool_id, "goal_reached")
    # 6. Expire AI suggestions, announce, bust check.
    with immediate(engine) as conn:
        now = clock.now()
        conn.execute(
            update(AiProposal)
            .where(AiProposal.status == "pending")
            .values(status="expired", reason="goal reached", decided_at=now)
        )
        top = leaderboard.standings(conn, season_id)[:3]
        enqueue(
            conn,
            clock,
            category=Category.GOAL_REACHED,
            payload={
                "kind": "goal_reached",
                "season": number,
                "start_x10": start_x10,
                "goal_x10": goal_x10,
                "value_x10": hit.value_x10 if hit else None,
                "day": hit.day if hit else None,
                "days": (now.astimezone(UTC) - started.astimezone(UTC)).days,
                "manual": actor is not None,
                "top": [{"user_id": s.user_id, "pnl_cents": s.pnl_cents} for s in top],
            },
            dedupe_key=f"goal_reached:{season_id}",
        )
        busts.check(conn, clock, season_id)
    result = GoalResult(season_id, settled, len(left), finished, refunded)
    log.info("goal_reached", season_id=season_id, manual=actor is not None, voided=len(left))
    return result


# ---- freeze / unfreeze -------------------------------------------------------------------


def set_frozen(engine: Engine, clock: Clock, actor: audit.Actor, frozen: bool) -> None:
    with immediate(engine) as conn:
        season = _season(conn)
        if not frozen and season is not None and season.status == "frozen":
            raise SeasonError(
                "goal_reached", "The goal was reached: start a new season instead of unfreezing."
            )
        before = instance.current_state(conn)
        target = instance.FROZEN if frozen else instance.ACTIVE
        if before == target:
            return
        locked = instance.set_instance_state(conn, clock, target)
        audit.record(
            conn,
            clock,
            actor,
            action="instance.freeze" if frozen else "instance.unfreeze",
            target=("settings", 1),
            before={"state": before},
            after={"state": target, "markets_locked": locked},
        )


# ---- new season --------------------------------------------------------------------------


def new_season(
    engine: Engine, clock: Clock, actor: audit.Actor, *, start_x10: int, goal_x10: int
) -> int:
    """End the current season and start the next with balances carried over (D-043)."""
    if start_x10 <= 0 or goal_x10 <= 0 or start_x10 == goal_x10:
        raise SeasonError("weights", "Enter a starting weight and a different goal weight.")
    with immediate(engine) as conn:
        if instance.current_state(conn) != instance.FROZEN:
            raise SeasonError("not_frozen", "Freeze the instance (or reach the goal) first.")
        old = _season(conn)
        open_markets = conn.execute(
            select(Market.id).where(
                Market.status.in_((MarketStatus.OPEN.value, MarketStatus.LOCKED.value))
            )
        ).first()
        open_pools = conn.execute(
            select(Pool.id).where(Pool.status.in_(("open", "locked")))
        ).first()
        if open_markets or open_pools:
            raise SeasonError(
                "unsettled",
                "Some markets or pools are still open or locked. Reach the goal (it settles "
                "or refunds everything) or void them first.",
            )
        now = clock.now()
        balances: dict[int, int] = {}
        if old is not None:
            balances = {
                user_id: balance
                for user_id, balance in conn.execute(
                    select(Account.user_id, Account.balance_cents).where(
                        Account.season_id == old.id, Account.kind == AccountKind.PLAYER.value
                    )
                )
                if user_id is not None
            }
            conn.execute(
                update(Season).where(Season.id == old.id).values(status="ended", ended_at=now)
            )
        season_id = ledger.open_season(conn, clock)
        conn.execute(
            update(Season)
            .where(Season.id == season_id)
            .values(
                start_weight_x10=start_x10,
                goal_weight_x10=goal_x10,
                direction="down" if goal_x10 < start_x10 else "up",
            )
        )
        players = (
            conn.execute(
                select(User.id)
                .where(User.role == "player", User.status != "banned")
                .order_by(User.id)
            )
            .scalars()
            .all()
        )
        carried = 0
        for user_id in players:
            account_id = ledger.open_player_account(conn, clock, season_id, user_id)
            amount = balances.get(user_id, 0)
            if amount > 0:
                ledger.carry_over(
                    conn,
                    clock,
                    account_id,
                    amount,
                    idempotency_key=f"season:{season_id}:carry:{user_id}",
                )
                carried += amount
        instance.set_instance_state(conn, clock, instance.ACTIVE)
        audit.record(
            conn,
            clock,
            actor,
            action="season.new",
            target=("season", season_id),
            before={"season_id": old.id if old else None},
            after={
                "start_x10": start_x10,
                "goal_x10": goal_x10,
                "players": len(players),
                "carried_cents": carried,
            },
        )
        number = conn.execute(select(Season.number).where(Season.id == season_id)).scalar_one()
        enqueue(
            conn,
            clock,
            category=Category.GOAL_REACHED,
            payload={
                "kind": "season_start",
                "season": number,
                "start_x10": start_x10,
                "goal_x10": goal_x10,
            },
            dedupe_key=f"season_start:{season_id}",
        )
    log.info("season_started", season_id=season_id, players=len(players))
    return season_id


@dataclass(frozen=True, slots=True)
class SeasonRow:
    season_id: int
    number: int
    status: str
    start_x10: int | None
    goal_x10: int | None
    direction: str | None
    started_at: datetime
    ended_at: datetime | None
    goal_reached_at: datetime | None


def history(conn: Connection) -> list[SeasonRow]:
    return [
        SeasonRow(
            s.id,
            s.number,
            s.status,
            s.start_weight_x10,
            s.goal_weight_x10,
            s.direction,
            s.started_at,
            s.ended_at,
            s.goal_reached_at,
        )
        for s in conn.execute(select(Season).order_by(Season.number.desc()))
    ]
