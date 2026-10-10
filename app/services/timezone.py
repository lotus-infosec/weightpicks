"""Changing the instance time zone after /setup (issue #30).

Days, weigh-in windows, drops and locks all depend on the zone, so everything still
open was priced on the old zone's days. A change therefore:

1. settles what complete data already decides (as Goal Reached does);
2. in one transaction pushes and refunds every open or locked market and pool, rejects
   markets awaiting approval, expires pending AI suggestions, switches the zone, writes
   the audit row and one Discord announcement;
3. re-drops today's daily markets in the new zone if today's drop time has passed and
   its lock hasn't. Weekly and monthly markets come back at their next scheduled drop:
   offering them mid-period would price a window whose first days are already known.

Readings need no rewrite: their days are worked out in the current zone.
The worker picks up the new zone on its next tick (`worker.main.reload_if_changed`).
"""

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import structlog
from sqlalchemy import Connection, Engine, func, select, update

from app.core.clock import Clock
from app.core.db import immediate
from app.domain.markets import MarketStatus, Timeframe
from app.domain.schedule import at_local, latest_daily, local_date
from app.models import AiProposal, Bet, BetLeg, InstanceSettingsRow, Market, Pool, PoolEntry
from app.services import admin, audit, busts, instance, markets, pools, settlement
from app.services.ledger import active_season_id
from app.services.outbox import Category, enqueue

log = structlog.get_logger()

LIVE = (MarketStatus.OPEN.value, MarketStatus.LOCKED.value)
LIVE_POOLS = ("open", "locked")
REASON = "time zone changed"


class TimezoneError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class Impact:
    """What a change to `zone` would do right now (shown before the admin confirms)."""

    current: str
    zone: str
    markets: int  # open or locked markets to push
    bets: int  # open single bets refunded
    stake_cents: int  # their stakes
    parlays: int  # parlays losing a leg (re-evaluated without it)
    pools: int  # open or locked pools refunded
    entries: int
    entry_cents: int
    redrop_daily: bool  # today's daily markets come back in the new zone

    @property
    def nothing_open(self) -> bool:
        return not (self.markets or self.pools)


@dataclass(frozen=True, slots=True)
class Result:
    zone: str
    settled: int
    voided_markets: int
    refunded_bets: int  # single bets; parlays lose the leg instead
    parlays: int
    refunded_pools: int
    redropped: int


def parse_zone(zone: str) -> str:
    name = zone.strip()
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise TimezoneError("unknown_zone", "Choose a time zone such as America/Chicago.") from exc
    if not name:
        raise TimezoneError("unknown_zone", "Choose a time zone such as America/Chicago.")
    return name


def _config(conn: Connection) -> instance.InstanceConfig:
    config = instance.read(conn)
    if config is None:
        raise TimezoneError("not_set_up", "Finish /setup first.")
    return config


def _redrop_due(config: instance.InstanceConfig, tz: ZoneInfo, now: datetime) -> bool:
    """Today's (new-zone) daily drop time has passed and its bet lock hasn't."""
    today = local_date(now, tz)
    return (
        config.state == instance.ACTIVE
        and latest_daily(now, config.schedule.daily_drop, tz) == today
        and now < at_local(today, config.schedule.bet_lock, tz)
    )


def preview(conn: Connection, zone: str, now: datetime) -> Impact:
    name = parse_zone(zone)
    config = _config(conn)
    live = select(Market.id).where(Market.status.in_(LIVE))
    n_markets = conn.execute(select(func.count()).select_from(live.subquery())).scalar_one()
    bets, stake = conn.execute(
        select(func.count(func.distinct(Bet.id)), func.coalesce(func.sum(Bet.stake_cents), 0))
        .join(BetLeg, BetLeg.bet_id == Bet.id)
        .where(
            BetLeg.market_id.in_(live),
            BetLeg.status == "open",
            Bet.kind == "single",
            Bet.status == "open",
        )
    ).one()
    parlays = conn.execute(
        select(func.count(func.distinct(Bet.id)))
        .join(BetLeg, BetLeg.bet_id == Bet.id)
        .where(
            BetLeg.market_id.in_(live),
            BetLeg.status == "open",
            Bet.kind == "parlay",
            Bet.status == "open",
        )
    ).scalar_one()
    live_pools = select(Pool.id).where(Pool.status.in_(LIVE_POOLS))
    n_pools = conn.execute(select(func.count()).select_from(live_pools.subquery())).scalar_one()
    entries, entry_cents = conn.execute(
        select(func.count(PoolEntry.id), func.coalesce(func.sum(Pool.buy_in_cents), 0))
        .join(Pool, Pool.id == PoolEntry.pool_id)
        .where(Pool.status.in_(LIVE_POOLS))
    ).one()
    return Impact(
        current=config.timezone,
        zone=name,
        markets=int(n_markets),
        bets=int(bets),
        stake_cents=int(stake),
        parlays=int(parlays),
        pools=int(n_pools),
        entries=int(entries),
        entry_cents=int(entry_cents),
        redrop_daily=name != config.timezone and _redrop_due(config, ZoneInfo(name), now),
    )


def change(engine: Engine, clock: Clock, actor: audit.Actor | None, zone: str) -> Result | None:
    """Switch to `zone`, pushing and refunding everything open. None if already set."""
    name = parse_zone(zone)
    with engine.connect() as conn:
        if _config(conn).timezone == name:
            return None
    # 1. What complete data already decides settles under the zone it was priced in.
    settled = settlement.settle_due(engine, clock).settled
    with engine.connect() as conn:
        locked_pools = conn.execute(select(Pool.id).where(Pool.status == "locked")).scalars().all()
    for pool_id in locked_pools:
        pools.settle_pool(engine, clock, pool_id)

    # 2. Push and refund the rest, and switch, in one transaction.
    with immediate(engine) as conn:
        config = _config(conn)
        before = config.timezone
        if before == name:  # changed by someone else in the meantime
            return None
        impact = preview(conn, name, clock.now())
        live = conn.execute(select(Market.id).where(Market.status.in_(LIVE)).order_by(Market.id))
        market_ids = list(live.scalars())
        for market_id in market_ids:
            admin.void_open_market(conn, clock, market_id, "timezone_changed", announce=False)
        pending = list(
            conn.execute(
                select(Market.id).where(Market.status == MarketStatus.PENDING_APPROVAL.value)
            ).scalars()
        )
        markets.set_status(
            conn, pending, MarketStatus.PENDING_APPROVAL, MarketStatus.REJECTED, clock.now()
        )
        conn.execute(
            update(AiProposal)
            .where(AiProposal.status == "pending")
            .values(status="expired", reason=REASON, decided_at=clock.now())
        )
        pool_ids = list(conn.execute(select(Pool.id).where(Pool.status.in_(LIVE_POOLS))).scalars())
        refunded_pools = sum(
            pools.refund_pool(conn, clock, p, "timezone_changed") for p in pool_ids
        )
        conn.execute(
            update(InstanceSettingsRow)
            .where(InstanceSettingsRow.id == 1)
            .values(timezone=name, updated_at=clock.now())
        )
        audit.record(
            conn,
            clock,
            actor,
            action="settings.timezone",
            target=("settings", 1),
            before={"timezone": before},
            after={
                "timezone": name,
                "markets_voided": len(market_ids),
                "bets_refunded": impact.bets,
                "parlays_affected": impact.parlays,
                "pools_refunded": refunded_pools,
            },
        )
        enqueue(
            conn,
            clock,
            category=Category.MARKET_SETTLEMENTS,
            payload={
                "kind": "timezone_changed",
                "from": before,
                "to": name,
                "markets": len(market_ids),
                "bets": impact.bets + impact.parlays,
                "pools": refunded_pools,
            },
            dedupe_key=f"timezone_changed:{before}>{name}:{clock.now().isoformat()}",
        )
        season_id = active_season_id(conn)
        if season_id is not None:
            busts.check(conn, clock, season_id)
        new_config = _config(conn)

    # 3. Today's daily markets, in the new zone.
    redropped = 0
    now = clock.now()
    tz = ZoneInfo(name)
    if _redrop_due(new_config, tz, now):
        drop = markets.drop(engine, clock, new_config, Timeframe.DAILY, local_date(now, tz))
        redropped = len(drop.created)
    log.info(
        "timezone_changed",
        before=before,
        after=name,
        markets=len(market_ids),
        bets=impact.bets,
        parlays=impact.parlays,
        pools=refunded_pools,
        redropped=redropped,
    )
    return Result(
        name, settled, len(market_ids), impact.bets, impact.parlays, refunded_pools, redropped
    )
