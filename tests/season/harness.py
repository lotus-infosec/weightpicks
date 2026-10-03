"""Simulated season v1 (STAGE06): scripted bettors over N simulated days.

Time moves only through `sim.advance`, i.e. the real worker jobs on the persisted
SimClock (sync -> drops -> lock -> settle). Bettors act twice a day, after the daily
drop (12:00) and in the evening (19:00), with a seeded RNG, and also try a handful of
bets that must be refused.
"""

from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
from sqlalchemy import Engine, select, update

from app.core.clock import SimClock
from app.core.config import Settings
from app.core.db import immediate, make_engine
from app.core.migrations import upgrade_to_head
from app.domain.markets import MarketStatus
from app.models import Account, Market, OddsVersion, Selection, User
from app.services import admin, auth, ledger, sim
from app.services.admin import Actor, AdminError
from app.services.bets import BetRejected, place_bet
from app.services.users import ensure_player

NY = ZoneInfo("America/New_York")
START = date(2026, 10, 1)
GRANT = 100_000
BET_TIMES = (time(12, 0), time(19, 0))


@dataclass
class SeasonRun:
    engine: Engine
    settings: Settings
    end: datetime
    players: list[int]
    placed: int = 0
    rejected: Counter[str] = field(default_factory=Counter)
    expected_rejections: Counter[str] = field(default_factory=Counter)
    bailouts: int = 0


def _open_selections(engine: Engine, now: datetime) -> list[tuple[int, int, str]]:
    """(selection_id, current odds version id, side) for every bettable side."""
    with engine.connect() as conn:
        rows = conn.execute(
            select(Selection.id, OddsVersion.id, Selection.side, OddsVersion.odds)
            .join(Market, Market.id == Selection.market_id)
            .join(OddsVersion, (OddsVersion.market_id == Market.id) & OddsVersion.is_current)
            .where(Market.status == MarketStatus.OPEN.value, Market.lock_at > now)
            .order_by(Selection.id)
        ).all()
    return [(s, v, side) for s, v, side, odds in rows if odds.get(side) is not None]


def _locked_selection(engine: Engine) -> tuple[int, int] | None:
    with engine.connect() as conn:
        row = conn.execute(
            select(Selection.id, OddsVersion.id)
            .join(Market, Market.id == Selection.market_id)
            .join(OddsVersion, OddsVersion.market_id == Market.id)
            .where(Market.status == MarketStatus.LOCKED.value)
            .limit(1)
        ).first()
    return None if row is None else (row[0], row[1])


def _balance(engine: Engine, user: int) -> int:
    with engine.connect() as conn:
        value: int = conn.execute(
            select(Account.balance_cents).where(Account.user_id == user)
        ).scalar_one()
    return value


def run_season(tmp_path: Path, *, days: int = 60, players: int = 6, seed: int = 11) -> SeasonRun:
    settings = Settings(
        app_env="dev",
        data_dir=tmp_path,
        data_provider="simulated",
        log_format="console",
        log_level="WARNING",
    )
    engine = make_engine(settings.db_url)
    upgrade_to_head(engine, tmp_path / ".migrate.lock")
    start = datetime.combine(START, time(0, 30), tzinfo=NY).astimezone(UTC)
    with immediate(engine) as conn:
        sim.ensure_state(conn, SimClock(start), seed=7, tz_name="America/New_York")
        season = ledger.open_season(conn, SimClock(start))
        users = []
        for n in range(1, players + 1):
            user = ensure_player(conn, SimClock(start), f"bettor{n}@example.invalid", f"B{n}")
            account = ledger.open_player_account(conn, SimClock(start), season, user)
            ledger.grant_starting(
                conn, SimClock(start), account, GRANT, idempotency_key=f"season:grant:{user}"
            )
            users.append(user)
    admin_id = auth.create_admin(
        engine,
        SimClock(start),
        email="admin@example.invalid",
        display_name="Admin",
        password="correct horse battery",
    )
    admin_actor = Actor(admin_id)
    run = SeasonRun(engine, settings, start + timedelta(days=days), users)
    rng = np.random.default_rng(seed)
    now = start

    def attempt(user: int, sel: int, ver: int, stake: int, key: str, expect: str | None) -> None:
        clock = SimClock(now)
        try:
            place_bet(
                engine,
                clock,
                user_id=user,
                selection_id=sel,
                odds_version_id=ver,
                stake_cents=stake,
                client_key=key,
            )
            run.placed += 1
            assert expect is None, f"expected {expect}"
        except BetRejected as exc:
            run.rejected[exc.reason] += 1
            if expect is not None:
                run.expected_rejections[exc.reason] += 1
                assert exc.reason == expect, (exc.reason, expect)

    for day in range(days):
        local_day = START + timedelta(days=day)
        for k, wall in enumerate(BET_TIMES):
            target = datetime.combine(local_day, wall, tzinfo=NY).astimezone(UTC)
            sim.advance(engine, settings, target - now)
            now = target
            options = _open_selections(engine, now)
            frozen = users[-1] if day > days // 2 or (day == days // 2 and k == 1) else None
            for user in users:
                if user == frozen or not options or rng.random() > 0.6:
                    continue
                sel, ver, _ = options[int(rng.integers(len(options)))]
                balance = _balance(engine, user)
                # users[1] is reckless: all-in every time, so busts and bailouts happen.
                fraction = 1.0 if user == users[1] else rng.uniform(0.01, 0.10)
                stake = max(100, int(balance * fraction))
                key = f"d{day}t{k}u{user}"
                attempt(
                    user, sel, ver, stake, key, None if balance >= stake else "insufficient_funds"
                )
            # Deliberate refusals, a few per week.
            if options and day % 3 == 0 and k == 0:
                sel, ver, _ = options[0]
                user = users[0]
                attempt(
                    user, sel, ver, _balance(engine, user) + 100, f"over{day}", "insufficient_funds"
                )
                attempt(user, sel, ver, 99, f"min{day}", "below_minimum")
                other = options[-1][1]
                if other != ver:
                    attempt(user, sel, other, 500, f"stale{day}", "stale_odds")
                locked = _locked_selection(engine)
                if locked:
                    attempt(user, locked[0], locked[1], 500, f"lock{day}", "locked")
            if day == days // 2 and k == 1:  # freeze the last bettor half-way through
                with immediate(engine) as conn:
                    conn.execute(update(User).where(User.id == users[-1]).values(status="frozen"))
            if day > days // 2 and options and k == 0:
                sel, ver, _ = options[0]
                attempt(users[-1], sel, ver, 500, f"frozen{day}", "user_inactive")
        # The admin bails out anyone whose cooldown has passed (D-037).
        for user in users:
            try:
                admin.bailout(engine, SimClock(now), admin_actor, user)
                run.bailouts += 1
            except AdminError as exc:
                assert exc.code in ("not_busted", "cooldown"), exc.code
    sim.advance(engine, settings, run.end - now)
    return run
