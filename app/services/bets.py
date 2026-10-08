"""Placing single bets (BUILD_PLAN §1.4.2).

Everything happens in one BEGIN IMMEDIATE transaction: validation, the bet and leg
rows, the `bet_stake` ledger entry and the outbox rows. A bet pins the odds version
it was placed at; the market must be open *and* before `lock_at` (the lock pass may
lag a tick, D-030). `client_key` makes a retried submit return the same bet.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Connection, Engine, insert, select

from app.core.clock import Clock
from app.core.db import immediate
from app.domain import parlay
from app.domain.economy import Economy
from app.domain.ledger import AccountKind, InsufficientFunds
from app.domain.markets import MarketStatus
from app.domain.money import payout_cents
from app.models import Account, Bet, BetLeg, Market, OddsVersion, Selection, User
from app.services import instance, ledger
from app.services.outbox import Category, enqueue

MIN_STAKE_CENTS = 100  # $1 (A9); the maximum and high-roller threshold are settings


class BetRejected(Exception):
    """A bet was refused. `reason` is a stable code for the UI and tests."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class PlacedBet:
    bet_id: int
    american: int
    line_x10: int | None
    stake_cents: int
    potential_payout_cents: int
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class _Leg:
    id: int  # market id
    side: str
    american: int
    line_x10: int | None
    keys: tuple[str, ...]


def _bettor(conn: Connection, user_id: int) -> tuple[int, int]:
    """(season_id, account_id) of an active player who may bet now."""
    user = conn.execute(select(User.role, User.status).where(User.id == user_id)).one_or_none()
    if user is None or user.role != "player" or user.status != "active":
        raise BetRejected("user_inactive")
    if instance.current_state(conn) == instance.FROZEN:
        raise BetRejected("instance_frozen")
    season_id = ledger.active_season_id(conn)
    account_id = (
        None
        if season_id is None
        else conn.execute(
            select(Account.id).where(
                Account.kind == AccountKind.PLAYER.value,
                Account.user_id == user_id,
                Account.season_id == season_id,
            )
        ).scalar_one_or_none()
    )
    if season_id is None or account_id is None:
        raise BetRejected("no_account")
    return season_id, account_id


def _leg(
    conn: Connection, selection_id: int, odds_version_id: int, season_id: int, now: datetime
) -> _Leg:
    """One selection that can be bet right now at the pinned (current) odds version."""
    market = conn.execute(
        select(
            Market.id,
            Market.status,
            Market.lock_at,
            Market.season_id,
            Market.correlation_keys,
            Selection.side,
        )
        .join(Selection, Selection.market_id == Market.id)
        .where(Selection.id == selection_id)
    ).one_or_none()
    if market is None or market.season_id != season_id:
        raise BetRejected("unknown_selection")
    if market.status != MarketStatus.OPEN.value or now >= market.lock_at:
        raise BetRejected("locked")
    version = conn.execute(
        select(
            OddsVersion.market_id,
            OddsVersion.is_current,
            OddsVersion.odds,
            OddsVersion.line_x10,
        ).where(OddsVersion.id == odds_version_id)
    ).one_or_none()
    if version is None or version.market_id != market.id or not version.is_current:
        raise BetRejected("stale_odds")
    american = version.odds.get(market.side)
    if american is None:
        raise BetRejected("side_not_offered")
    return _Leg(market.id, market.side, american, version.line_x10, tuple(market.correlation_keys))


def _check_stake(conn: Connection, stake_cents: int) -> Economy:
    if stake_cents < MIN_STAKE_CENTS:
        raise BetRejected("below_minimum", f"minimum stake is {MIN_STAKE_CENTS} cents")
    economy = instance.economy(conn)
    if economy.max_bet_cents is not None and stake_cents > economy.max_bet_cents:
        raise BetRejected("above_maximum", f"maximum stake is {economy.max_bet_cents} cents")
    return economy


def place_bet(
    engine: Engine,
    clock: Clock,
    *,
    user_id: int,
    selection_id: int,
    odds_version_id: int,
    stake_cents: int,
    client_key: str,
) -> PlacedBet:
    if type(stake_cents) is not int:
        raise BetRejected("invalid_stake", "stake must be integer cents")
    if not client_key or len(client_key) > 64:
        raise BetRejected("invalid_client_key")
    now = clock.now()
    with immediate(engine) as conn:
        prior = conn.execute(
            select(
                Bet.id,
                Bet.stake_cents,
                Bet.potential_payout_cents,
                BetLeg.selection_id,
                BetLeg.odds_version_id,
                BetLeg.american,
                BetLeg.line_x10,
            )
            .join(BetLeg, BetLeg.bet_id == Bet.id)
            .where(Bet.user_id == user_id, Bet.client_key == client_key)
        ).one_or_none()
        if prior is not None:
            if (prior.selection_id, prior.odds_version_id, prior.stake_cents) != (
                selection_id,
                odds_version_id,
                stake_cents,
            ):
                raise BetRejected("client_key_conflict")
            return PlacedBet(
                prior.id,
                prior.american,
                prior.line_x10,
                prior.stake_cents,
                prior.potential_payout_cents,
                replayed=True,
            )

        season_id, account_id = _bettor(conn, user_id)
        leg = _leg(conn, selection_id, odds_version_id, season_id, now)
        american = leg.american
        economy = _check_stake(conn, stake_cents)

        potential = payout_cents(stake_cents, american)
        bet_id = conn.execute(
            insert(Bet)
            .values(
                user_id=user_id,
                season_id=season_id,
                account_id=account_id,
                kind="single",
                stake_cents=stake_cents,
                potential_payout_cents=potential,
                status="open",
                placed_at=now,
                client_key=client_key,
            )
            .returning(Bet.id)
        ).scalar_one()
        conn.execute(
            insert(BetLeg).values(
                bet_id=bet_id,
                market_id=leg.id,
                selection_id=selection_id,
                odds_version_id=odds_version_id,
                american=american,
                line_x10=leg.line_x10,
                status="open",
            )
        )
        try:
            ledger.stake(
                conn,
                clock,
                account_id,
                stake_cents,
                idempotency_key=f"bet:{bet_id}:stake",
                ref_id=bet_id,
            )
        except InsufficientFunds as exc:
            raise BetRejected("insufficient_funds") from exc
        payload = {
            "bet_id": bet_id,
            "user_id": user_id,
            "market_id": leg.id,
            "side": leg.side,
            "american": american,
            "line_x10": leg.line_x10,
            "stake_cents": stake_cents,
        }
        enqueue(
            conn,
            clock,
            category=Category.BETS_PLACED,
            payload=payload,
            dedupe_key=f"bet_placed:{bet_id}",
        )
        if stake_cents >= economy.high_roller_cents:
            enqueue(
                conn,
                clock,
                category=Category.HIGH_ROLLER,
                payload=payload,
                dedupe_key=f"high_roller:{bet_id}",
            )
    return PlacedBet(bet_id, american, leg.line_x10, stake_cents, potential)


@dataclass(frozen=True, slots=True)
class PlacedParlay:
    bet_id: int
    legs: int
    combined_american: int
    stake_cents: int
    potential_payout_cents: int
    replayed: bool = False


def place_parlay(
    engine: Engine,
    clock: Clock,
    *,
    user_id: int,
    legs: list[tuple[int, int]],
    stake_cents: int,
    client_key: str,
) -> PlacedParlay:
    """A parlay of 2..max_parlay_legs singles at their pinned odds (BUILD_PLAN §1.4.2,
    D-041). Refused if two legs share a market or their correlation keys intersect, or
    if the potential payout would exceed 100x the stake."""
    if type(stake_cents) is not int:
        raise BetRejected("invalid_stake", "stake must be integer cents")
    if not client_key or len(client_key) > 64:
        raise BetRejected("invalid_client_key")
    now = clock.now()
    with immediate(engine) as conn:
        prior = conn.execute(
            select(Bet.id, Bet.kind, Bet.stake_cents, Bet.potential_payout_cents).where(
                Bet.user_id == user_id, Bet.client_key == client_key
            )
        ).one_or_none()
        if prior is not None:
            pinned = conn.execute(
                select(BetLeg.selection_id, BetLeg.odds_version_id, BetLeg.american)
                .where(BetLeg.bet_id == prior.id)
                .order_by(BetLeg.id)
            ).all()
            if (
                prior.kind != "parlay"
                or prior.stake_cents != stake_cents
                or [(p.selection_id, p.odds_version_id) for p in pinned] != list(legs)
            ):
                raise BetRejected("client_key_conflict")
            return PlacedParlay(
                prior.id,
                len(pinned),
                parlay.combined_american([p.american for p in pinned]),
                prior.stake_cents,
                prior.potential_payout_cents,
                replayed=True,
            )

        config = instance.read(conn)
        if config is None or not config.flags.get("parlays"):
            raise BetRejected("parlays_off")
        season_id, account_id = _bettor(conn, user_id)
        max_legs = config.economy.max_parlay_legs
        if not parlay.MIN_LEGS <= len(legs) <= max_legs:
            raise BetRejected("leg_count", f"a parlay has {parlay.MIN_LEGS} to {max_legs} legs")
        picked = [_leg(conn, sel, ver, season_id, now) for sel, ver in legs]
        if len({leg.id for leg in picked}) < len(picked):
            raise BetRejected("same_market")
        if parlay.correlated([leg.keys for leg in picked]) is not None:
            raise BetRejected("correlated")
        economy = _check_stake(conn, stake_cents)
        odds = [leg.american for leg in picked]
        potential = parlay.potential_payout_cents(stake_cents, odds)
        if parlay.over_cap(stake_cents, potential):
            raise BetRejected(
                "over_cap", f"a parlay can pay at most {parlay.PAYOUT_CAP_MULTIPLE}x the stake"
            )
        bet_id = conn.execute(
            insert(Bet)
            .values(
                user_id=user_id,
                season_id=season_id,
                account_id=account_id,
                kind="parlay",
                stake_cents=stake_cents,
                potential_payout_cents=potential,
                status="open",
                placed_at=now,
                client_key=client_key,
            )
            .returning(Bet.id)
        ).scalar_one()
        conn.execute(
            insert(BetLeg),
            [
                {
                    "bet_id": bet_id,
                    "market_id": leg.id,
                    "selection_id": sel,
                    "odds_version_id": ver,
                    "american": leg.american,
                    "line_x10": leg.line_x10,
                    "status": "open",
                }
                for leg, (sel, ver) in zip(picked, legs, strict=True)
            ],
        )
        try:
            ledger.stake(
                conn,
                clock,
                account_id,
                stake_cents,
                idempotency_key=f"bet:{bet_id}:stake",
                ref_id=bet_id,
            )
        except InsufficientFunds as exc:
            raise BetRejected("insufficient_funds") from exc
        price = parlay.combined_american(odds)
        payload = {
            "bet_id": bet_id,
            "user_id": user_id,
            "kind": "parlay",
            "legs": len(picked),
            "american": price,
            "stake_cents": stake_cents,
            "market_ids": [leg.id for leg in picked],
        }
        enqueue(
            conn,
            clock,
            category=Category.BETS_PLACED,
            payload=payload,
            dedupe_key=f"bet_placed:{bet_id}",
        )
        if stake_cents >= economy.high_roller_cents:
            enqueue(
                conn,
                clock,
                category=Category.HIGH_ROLLER,
                payload=payload,
                dedupe_key=f"high_roller:{bet_id}",
            )
    return PlacedParlay(bet_id, len(picked), price, stake_cents, potential)
