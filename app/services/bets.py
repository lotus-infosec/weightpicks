"""Placing single bets (BUILD_PLAN §1.4.2, STAGE06).

Everything happens in one BEGIN IMMEDIATE transaction: validation, the bet and leg
rows, the `bet_stake` ledger entry and the outbox rows. A bet pins the odds version
it was placed at; the market must be open *and* before `lock_at` (the lock pass may
lag a tick, D-030). `client_key` makes a retried submit return the same bet.
"""

from dataclasses import dataclass

from sqlalchemy import Engine, insert, select

from app.core.clock import Clock
from app.core.db import immediate
from app.domain.ledger import AccountKind, InsufficientFunds
from app.domain.markets import MarketStatus
from app.domain.money import payout_cents
from app.models import Account, Bet, BetLeg, Market, OddsVersion, Selection, User
from app.services import instance, ledger
from app.services.outbox import Category, enqueue

MIN_STAKE_CENTS = 100  # $1 (A9); no maximum (CONCEPT §6)
HIGH_ROLLER_CENTS = 50_000  # $500 (CONCEPT §6); a setting from STAGE08


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

        market = conn.execute(
            select(Market.id, Market.status, Market.lock_at, Market.season_id, Selection.side)
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
        if stake_cents < MIN_STAKE_CENTS:
            raise BetRejected("below_minimum", f"minimum stake is {MIN_STAKE_CENTS} cents")

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
                market_id=market.id,
                selection_id=selection_id,
                odds_version_id=odds_version_id,
                american=american,
                line_x10=version.line_x10,
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
            "market_id": market.id,
            "side": market.side,
            "american": american,
            "line_x10": version.line_x10,
            "stake_cents": stake_cents,
        }
        enqueue(
            conn,
            clock,
            category=Category.BETS_PLACED,
            payload=payload,
            dedupe_key=f"bet_placed:{bet_id}",
        )
        if stake_cents >= HIGH_ROLLER_CENTS:
            enqueue(
                conn,
                clock,
                category=Category.HIGH_ROLLER,
                payload=payload,
                dedupe_key=f"high_roller:{bet_id}",
            )
    return PlacedBet(bet_id, american, version.line_x10, stake_cents, potential)
