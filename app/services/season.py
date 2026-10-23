"""Goal Reached, freeze and new seasons.

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
from sqlalchemy import Connection, Engine, func, insert, select, update
from sqlalchemy.exc import IntegrityError

from app.core.clock import Clock
from app.core.db import immediate
from app.domain.ledger import AccountKind
from app.domain.markets import MarketStatus
from app.domain.schedule import local_date
from app.models import Account, AiProposal, JobRun, Market, Pool, Season, User
from app.services import (
    admin,
    audit,
    busts,
    instance,
    leaderboard,
    ledger,
    markets,
    pools,
    settlement,
)
from app.services.observations import canonical_weigh_ins, latest_complete_through
from app.services.outbox import Category, enqueue

log = structlog.get_logger()

GOAL_JOB = "goal_reached"


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
    """Run Goal Reached for the current season. None if it already ran."""
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
        # 1. Freeze and lock every open market so nothing can slip in, and so markets the
        # goal weigh-in already decides (milestones) settle in step 2.
        instance.set_instance_state(conn, clock, instance.FROZEN)
        markets.lock_open_markets(conn, now, everything=True)
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
    settled, left, finished, refunded = _resolve_open(engine, clock, season_id, "goal_reached")
    # 6. Expire AI suggestions, announce, bust check.
    with immediate(engine) as conn:
        now = clock.now()
        conn.execute(
            update(AiProposal)
            .where(AiProposal.status == "pending")
            .values(status="expired", reason="goal reached", decided_at=now)
        )
        top = leaderboard.ranked(leaderboard.standings(conn, season_id))[:3]
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


def _resolve_open(
    engine: Engine, clock: Clock, season_id: int, reason: str
) -> tuple[int, list[int], int, int]:
    """Goal Reached and starting a new season: settle every market and special event the
    data already decides, then void and refund the rest with `reason`. Returns (markets
    settled, market ids voided, events settled, events refunded)."""
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
            admin.void_open_market(conn, clock, market_id, reason)
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
            refunded += pools.refund_pool(conn, clock, pool_id, reason)
    return settled, list(left), finished, refunded


def set_frozen(engine: Engine, clock: Clock, actor: audit.Actor, frozen: bool) -> int:
    """Pause or resume betting. Open markets stay open and lock at their own time
    (issue #59); unfreezing posts today's drops that were skipped while frozen. Returns
    how many markets that posted."""
    with immediate(engine) as conn:
        season = _season(conn)
        if not frozen and season is not None and season.status == "frozen":
            raise SeasonError(
                "goal_reached", "The goal was reached: start a new season instead of unfreezing."
            )
        before = instance.current_state(conn)
        target = instance.FROZEN if frozen else instance.ACTIVE
        if before == target:
            return 0
        instance.set_instance_state(conn, clock, target)
        audit.record(
            conn,
            clock,
            actor,
            action="instance.freeze" if frozen else "instance.unfreeze",
            target=("settings", 1),
            before={"state": before},
            after={"state": target},
        )
        config = instance.read(conn)
    if frozen or config is None:
        return 0
    return markets.catch_up_drops(engine, clock, config)


# ---- new season --------------------------------------------------------------------------


def unsettled(conn: Connection) -> tuple[int, int]:
    """(markets, special events) still open or locked: what blocks a new season."""
    live = (MarketStatus.OPEN.value, MarketStatus.LOCKED.value)
    markets = conn.execute(select(func.count()).where(Market.status.in_(live))).scalar_one()
    events = conn.execute(
        select(func.count()).where(Pool.status.in_(("open", "locked")))
    ).scalar_one()
    return int(markets), int(events)


def describe_unsettled(markets: int, events: int) -> str:
    """ "3 markets and 1 special event" (issue #85: say what is left, not "some")."""
    parts = [f"{markets} market{'s' if markets != 1 else ''}"] if markets else []
    if events:
        parts.append(f"{events} special event{'s' if events != 1 else ''}")
    return " and ".join(parts)


def new_season(
    engine: Engine,
    clock: Clock,
    actor: audit.Actor,
    *,
    start_x10: int,
    goal_x10: int,
    void_open: bool = False,
) -> int:
    """End the current season and start the next with balances carried over.
    `void_open` (the admin ticked it, issue #85) first settles whatever the data already
    decides and voids and refunds every other open market and special event."""
    if start_x10 <= 0 or goal_x10 <= 0 or start_x10 == goal_x10:
        raise SeasonError("weights", "Enter a starting weight and a different goal weight.")
    with engine.connect() as conn:
        if instance.current_state(conn) != instance.FROZEN:
            raise SeasonError("not_frozen", "Freeze the instance (or reach the goal) first.")
        current = _season(conn)
    resolved: tuple[int, list[int], int, int] | None = None
    if void_open and current is not None:
        resolved = _resolve_open(engine, clock, current.id, "season_ended")
    with immediate(engine) as conn:
        if instance.current_state(conn) != instance.FROZEN:
            raise SeasonError("not_frozen", "Freeze the instance (or reach the goal) first.")
        old = _season(conn)
        markets, events = unsettled(conn)
        if markets or events:
            raise SeasonError(
                "unsettled",
                f"{describe_unsettled(markets, events)} "
                f"{'is' if markets + events == 1 else 'are'} still open or locked. Tick "
                '"Void and refund everything still open" to end them now, or void them '
                "first (Markets, Events).",
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
            }
            | (
                {
                    "settled_markets": resolved[0],
                    "voided_markets": len(resolved[1]),
                    "settled_events": resolved[2],
                    "refunded_events": resolved[3],
                }
                if resolved
                else {}
            ),
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
