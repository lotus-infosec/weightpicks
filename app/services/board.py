"""Read models for the player pages: the board, a market, my bets, the public feed.

Read-only queries; nothing here writes. The feed and market pages show display names
only, never emails (bets are public by design, BUILD_PLAN §1.3).
"""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy import Connection, case, select

from app.domain import parlay
from app.domain.ledger import AccountKind
from app.domain.markets import MarketStatus
from app.models import (
    Account,
    Bet,
    BetLeg,
    Market,
    OddsVersion,
    Pool,
    Selection,
    Settlement,
    User,
)
from app.services.ledger import active_season_id
from app.services.pools import entries_by_pool


@dataclass(frozen=True, slots=True)
class Side:
    selection_id: int
    side: str
    odds: int | None  # None = not offered


@dataclass(frozen=True, slots=True)
class MarketCard:
    market_id: int
    title: str
    template: str
    timeframe: str
    metric: str
    status: str
    lock_at: datetime
    line_x10: int | None
    odds_version_id: int
    sides: tuple[Side, ...]
    provisional: bool
    correlation_keys: tuple[str, ...] = ()
    blurb: str | None = None  # AI flavour text; the title is the binding terms
    origin: str = "core"


@dataclass(frozen=True, slots=True)
class LegRow:
    market_id: int
    title: str
    metric: str
    side: str
    american: int
    line_x10: int | None
    status: str


@dataclass(frozen=True, slots=True)
class BetRow:
    bet_id: int
    display_name: str
    market_id: int
    title: str
    metric: str
    side: str
    american: int
    line_x10: int | None
    stake_cents: int
    potential_payout_cents: int
    status: str
    payout_cents: int | None
    placed_at: datetime
    kind: str = "single"
    legs: tuple[LegRow, ...] = ()


@dataclass(frozen=True, slots=True)
class Wallet:
    balance_cents: int
    pnl_cents: int


def _cards(conn: Connection, *conditions: Any) -> list[MarketCard]:
    rows = conn.execute(
        select(
            Market.id,
            Market.title,
            Market.template,
            Market.timeframe,
            Market.metric,
            Market.status,
            Market.lock_at,
            OddsVersion.id.label("version_id"),
            OddsVersion.line_x10,
            OddsVersion.odds,
            OddsVersion.model_inputs,
            Market.correlation_keys,
            Market.blurb,
            Market.origin,
        )
        .join(OddsVersion, (OddsVersion.market_id == Market.id) & OddsVersion.is_current)
        .where(*conditions)
        .order_by(Market.lock_at, Market.id)
    ).all()
    selections: dict[int, list[tuple[int, str]]] = {}
    if rows:
        for market_id, sel_id, side in conn.execute(
            select(Selection.market_id, Selection.id, Selection.side)
            .where(Selection.market_id.in_([r.id for r in rows]))
            .order_by(Selection.id)
        ):
            selections.setdefault(market_id, []).append((sel_id, side))
    return [
        MarketCard(
            market_id=r.id,
            title=r.title,
            template=r.template,
            timeframe=r.timeframe,
            metric=r.metric,
            status=r.status,
            lock_at=r.lock_at,
            line_x10=r.line_x10,
            odds_version_id=r.version_id,
            sides=tuple(Side(s, side, r.odds.get(side)) for s, side in selections.get(r.id, [])),
            provisional=bool(r.model_inputs.get("provisional")),
            correlation_keys=tuple(r.correlation_keys or ()),
            blurb=r.blurb,
            origin=r.origin,
        )
        for r in rows
    ]


def open_markets(conn: Connection, timeframe: str, now: datetime) -> list[MarketCard]:
    season = active_season_id(conn)
    return _cards(
        conn,
        Market.season_id == season,
        Market.timeframe == timeframe,
        Market.status == MarketStatus.OPEN.value,
        Market.lock_at > now,
    )


def market(conn: Connection, market_id: int) -> tuple[MarketCard, dict[str, Any] | None] | None:
    cards = _cards(conn, Market.id == market_id)
    if not cards:
        return None
    outcome = conn.execute(
        select(Settlement.outcome).where(Settlement.market_id == market_id)
    ).scalar_one_or_none()
    return cards[0], outcome


def _bets(conn: Connection, *conditions: Any, limit: int = 50) -> list[BetRow]:
    """One row per bet, newest first; a parlay carries its legs (leg 1 fills the single
    fields so older templates still render)."""
    wanted = (
        select(Bet.id)
        .join(BetLeg, BetLeg.bet_id == Bet.id)
        .where(*conditions)
        .group_by(Bet.id)
        .order_by(Bet.placed_at.desc(), Bet.id.desc())
        .limit(limit)
        .scalar_subquery()
    )
    rows = conn.execute(
        select(
            Bet.id.label("bet_id"),
            case((User.status == "banned", "Removed player"), else_=User.display_name).label(
                "display_name"
            ),
            Market.id.label("market_id"),
            Market.title,
            Market.metric,
            Selection.side,
            BetLeg.american,
            BetLeg.line_x10,
            BetLeg.status.label("leg_status"),
            Bet.stake_cents,
            Bet.potential_payout_cents,
            Bet.status,
            Bet.payout_cents,
            Bet.placed_at,
            Bet.kind,
        )
        .join(BetLeg, BetLeg.bet_id == Bet.id)
        .join(Market, Market.id == BetLeg.market_id)
        .join(Selection, Selection.id == BetLeg.selection_id)
        .join(User, User.id == Bet.user_id)
        .where(Bet.id.in_(wanted))
        .order_by(Bet.placed_at.desc(), Bet.id.desc(), BetLeg.id)
    ).all()
    grouped: dict[int, list[Any]] = {}
    for r in rows:
        grouped.setdefault(r.bet_id, []).append(r)
    out: list[BetRow] = []
    for legs in grouped.values():
        first = legs[0]
        leg_rows = tuple(
            LegRow(r.market_id, r.title, r.metric, r.side, r.american, r.line_x10, r.leg_status)
            for r in legs
        )
        is_parlay = first.kind == "parlay"
        out.append(
            BetRow(
                bet_id=first.bet_id,
                display_name=first.display_name,
                market_id=first.market_id,
                title=f"{len(legs)}-leg parlay" if is_parlay else first.title,
                metric=first.metric,
                side=first.side,
                american=parlay.combined_american([r.american for r in legs])
                if is_parlay
                else first.american,
                line_x10=first.line_x10,
                stake_cents=first.stake_cents,
                potential_payout_cents=first.potential_payout_cents,
                status=first.status,
                payout_cents=first.payout_cents,
                placed_at=first.placed_at,
                kind=first.kind,
                legs=leg_rows,
            )
        )
    return out


def my_bets(conn: Connection, user_id: int, season_id: int | None = None) -> list[BetRow]:
    """A player's bets; with `season_id`, only that season's (the season switcher)."""
    if season_id is None:
        return _bets(conn, Bet.user_id == user_id, limit=200)
    return _bets(conn, Bet.user_id == user_id, Bet.season_id == season_id, limit=200)


def feed(conn: Connection, limit: int = 50) -> list[BetRow]:
    return _bets(conn, Bet.season_id == active_season_id(conn), limit=limit)


def market_bets(conn: Connection, market_id: int) -> list[BetRow]:
    return _bets(conn, BetLeg.market_id == market_id, limit=500)


def wallet(conn: Connection, user_id: int) -> Wallet | None:
    season = active_season_id(conn)
    row = conn.execute(
        select(Account.balance_cents, Account.pnl_cents).where(
            Account.kind == AccountKind.PLAYER.value,
            Account.user_id == user_id,
            Account.season_id == season,
        )
    ).one_or_none()
    return None if row is None else Wallet(row.balance_cents, row.pnl_cents)


# ---- special events (pools, D-043) --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PoolCard:
    pool_id: int
    title: str
    question: str
    target_date: date
    buy_in_cents: int
    lock_at: datetime
    status: str  # open | locked | settled | refunded
    entries: int
    pot_cents: int
    my_guess_x10: int | None
    result_x10: int | None
    # Everyone's guesses, shown only once the pool is locked (hidden while it's open).
    guesses: tuple[tuple[str, int, int | None], ...] = ()  # (name, guess, payout)

    @property
    def open(self) -> bool:
        return self.status == "open"


def pools(
    conn: Connection, user_id: int, now: datetime, season_id: int | None = None
) -> list[PoolCard]:
    """This season's pools for the Events tab: open ones first, then recent results."""
    season_id = season_id or active_season_id(conn)
    rows = conn.execute(
        select(Pool)
        .where(Pool.season_id == season_id)
        .order_by((Pool.status == "open").desc(), Pool.target_date, Pool.id.desc())
        .limit(20)
    ).all()
    cards: list[PoolCard] = []
    all_entries = entries_by_pool(conn, [p.id for p in rows])
    for p in rows:
        entries = all_entries[p.id]
        mine = next((e.guess_x10 for e in entries if e.user_id == user_id), None)
        status = "locked" if p.status == "open" and now >= p.lock_at else p.status
        shown = (
            tuple(
                (
                    "Removed player" if e.status == "banned" else e.display_name,  # D-012
                    e.guess_x10,
                    e.payout_cents,
                )
                for e in entries
            )
            if status != "open"
            else ()
        )
        cards.append(
            PoolCard(
                p.id,
                p.title,
                p.question,
                p.target_date,
                p.buy_in_cents,
                p.lock_at,
                status,
                len(entries),
                p.buy_in_cents * len(entries),
                mine,
                p.result_x10,
                shown,
            )
        )
    return cards
