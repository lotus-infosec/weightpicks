"""Leaderboard: players ranked by settled P&L, then wins. Removed (banned) players are
hidden. Read-only.

Public P&L counts **finished** bets and events only (issue #42): an open stake isn't a
loss yet, so placing a bet never moves anyone's standing, and the size of open bets can't
be read off the board. It equals the ledger's betting P&L plus open stakes and buy-ins;
the ledger itself (and `wp ledger verify`) is unchanged."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, func, select

from app.domain.ledger import AccountKind
from app.models import Account, Bet, Pool, PoolEntry, Season, User
from app.services.ledger import active_season_id


@dataclass(frozen=True, slots=True)
class Standing:
    rank: int
    user_id: int
    display_name: str
    pnl_cents: int  # settled P&L
    wins: int  # bets won plus events won, this season
    open_bets: int


@dataclass(frozen=True, slots=True)
class SeasonOption:
    season_id: int
    number: int
    current: bool


def seasons(conn: Connection) -> list[SeasonOption]:
    current = active_season_id(conn)
    return [
        SeasonOption(sid, number, sid == current)
        for sid, number in conn.execute(
            select(Season.id, Season.number).order_by(Season.number.desc())
        )
    ]


def _window(column: Any, since: datetime | None, until: datetime | None) -> list[Any]:
    return [c for c in (since and column >= since, until and column < until) if c is not None]


def settled_pnl(
    conn: Connection,
    season_id: int,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    user_id: int | None = None,
) -> dict[int, int]:
    """Per player: what finished bets and events returned minus what they cost. With
    `since`/`until`, only those that finished in that window (the weekly change)."""
    totals: dict[int, int] = {}
    bets = conn.execute(
        select(Bet.user_id, func.sum(func.coalesce(Bet.payout_cents, 0) - Bet.stake_cents))
        .where(
            Bet.season_id == season_id,
            Bet.status != "open",
            *_window(Bet.settled_at, since, until),
            *([Bet.user_id == user_id] if user_id is not None else []),
        )
        .group_by(Bet.user_id)
    )
    for user_id, cents in bets:
        totals[user_id] = totals.get(user_id, 0) + int(cents)
    entries = conn.execute(  # a refunded pot nets to zero, so only settled ones count
        select(
            PoolEntry.user_id,
            func.sum(func.coalesce(PoolEntry.payout_cents, 0) - Pool.buy_in_cents),
        )
        .join(Pool, Pool.id == PoolEntry.pool_id)
        .where(
            Pool.season_id == season_id,
            Pool.status == "settled",
            *_window(Pool.settled_at, since, until),
            *([PoolEntry.user_id == user_id] if user_id is not None else []),
        )
        .group_by(PoolEntry.user_id)
    )
    for user_id, cents in entries:
        totals[user_id] = totals.get(user_id, 0) + int(cents)
    return totals


def wins(conn: Connection, season_id: int) -> dict[int, int]:
    """Per player: bets won (singles and parlays) plus events won, this season."""
    totals: dict[int, int] = {}
    won_bets = conn.execute(
        select(Bet.user_id, func.count())
        .where(Bet.season_id == season_id, Bet.status == "won")
        .group_by(Bet.user_id)
    )
    won_pools = conn.execute(
        select(PoolEntry.user_id, func.count())
        .join(Pool, Pool.id == PoolEntry.pool_id)
        .where(Pool.season_id == season_id, Pool.status == "settled", PoolEntry.payout_cents > 0)
        .group_by(PoolEntry.user_id)
    )
    for user_id, n in [*won_bets, *won_pools]:
        totals[user_id] = totals.get(user_id, 0) + int(n)
    return totals


def standings(conn: Connection, season_id: int | None) -> list[Standing]:
    if season_id is None:
        return []
    open_bets = dict(
        conn.execute(
            select(Bet.user_id, func.count())
            .where(Bet.status == "open", Bet.season_id == season_id)
            .group_by(Bet.user_id)
        ).all()
    )
    players = conn.execute(
        select(User.id, User.display_name)
        .join(Account, Account.user_id == User.id)
        .where(
            Account.season_id == season_id,
            Account.kind == AccountKind.PLAYER.value,
            User.role == "player",
            User.status != "banned",
        )
    ).all()
    pnl = settled_pnl(conn, season_id)
    won = wins(conn, season_id)
    ranked = sorted(players, key=lambda p: (-pnl.get(p.id, 0), -won.get(p.id, 0), p.display_name))
    return [
        Standing(
            i, p.id, p.display_name, pnl.get(p.id, 0), won.get(p.id, 0), open_bets.get(p.id, 0)
        )
        for i, p in enumerate(ranked, 1)
    ]
