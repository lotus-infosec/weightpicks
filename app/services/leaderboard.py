"""Leaderboard: players ranked by season P&L (betting results only), then balance
. Removed (banned) players are hidden (C5). Read-only."""

from dataclasses import dataclass

from sqlalchemy import Connection, func, select

from app.domain.ledger import AccountKind
from app.models import Account, Bet, Season, User
from app.services import busts
from app.services.ledger import active_season_id


@dataclass(frozen=True, slots=True)
class Standing:
    rank: int
    user_id: int
    display_name: str
    pnl_cents: int
    balance_cents: int
    busts: int
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


def standings(conn: Connection, season_id: int | None) -> list[Standing]:
    if season_id is None:
        return []
    open_bets = (
        select(Bet.user_id, func.count().label("n"))
        .where(Bet.status == "open", Bet.season_id == season_id)
        .group_by(Bet.user_id)
        .subquery()
    )
    rows = conn.execute(
        select(
            User.id,
            User.display_name,
            Account.pnl_cents,
            Account.balance_cents,
            func.coalesce(open_bets.c.n, 0),
        )
        .join(Account, Account.user_id == User.id)
        .outerjoin(open_bets, open_bets.c.user_id == User.id)
        .where(
            Account.season_id == season_id,
            Account.kind == AccountKind.PLAYER.value,
            User.role == "player",
            User.status != "banned",
        )
        .order_by(Account.pnl_cents.desc(), Account.balance_cents.desc(), User.display_name)
    ).all()
    badges = busts.badge_counts(conn, season_id)
    return [
        Standing(i, uid, name, pnl, balance, badges.get(uid, 0), n)
        for i, (uid, name, pnl, balance, n) in enumerate(rows, 1)
    ]
