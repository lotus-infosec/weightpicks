"""Issue #30 / D-049: changing the time zone pushes and refunds everything open, switches
the zone (no restart), re-drops today's daily markets in the new zone, and posts once."""

from datetime import date
from typing import Any

import pytest
from sqlalchemy import func, select

from app.domain.markets import Timeframe
from app.models import AuditEntry, Bet, Market, OutboxMessage, Pool
from app.services import instance, ledger, markets, timezone
from app.worker.jobs import domain_jobs
from app.worker.main import reload_if_changed
from tests.integration.test_bets_settlement import account, bet, player, weight_market
from tests.integration.test_season import admin_actor, mid_season  # noqa: F401
from tests.integration.world import World, local, sync_sim

CHICAGO = "America/Chicago"


def _impact(w: World, zone: str = CHICAGO) -> timezone.Impact:
    with w.engine.connect() as conn:
        return timezone.preview(conn, zone, w.clock.now())


def test_preview_then_change_refunds_everything(mid_season: dict[str, Any]) -> None:  # noqa: F811
    w, m = mid_season["world"], mid_season["m"]
    p1, p2 = mid_season["p1"], mid_season["p2"]
    w.clock.set(local(2026, 10, 5, 12, 30))  # 11:30 in Chicago: after the daily drop
    impact = _impact(w)
    assert (impact.current, impact.markets, impact.bets, impact.parlays) == (
        "America/New_York",
        4,
        4,
        1,
    )
    assert (impact.pools, impact.entries, impact.entry_cents) == (2, 4, 8_000)
    assert impact.redrop_daily and not impact.nothing_open

    result = timezone.change(w.engine, w.clock, admin_actor(w), CHICAGO)
    assert result is not None
    assert (result.voided_markets, result.refunded_bets, result.parlays) == (4, 4, 1)
    assert result.refunded_pools == 2
    assert result.redropped > 0
    with w.engine.connect() as conn:
        config = instance.read(conn)
        assert config is not None and config.timezone == CHICAGO
        assert ledger.verify(conn).ok
        old = conn.execute(select(Market.status).where(Market.id.in_(m.values()))).scalars().all()
        parlay = conn.execute(select(Bet.status).where(Bet.id == mid_season["parlay"])).scalar_one()
        pool_status = conn.execute(select(Pool.status)).scalars().all()
        new = conn.execute(
            select(Market.dedupe_key, Market.status).where(Market.id.not_in(m.values()))
        ).all()
        posts = conn.execute(
            select(OutboxMessage.category, OutboxMessage.payload, OutboxMessage.dedupe_key)
        ).all()
        audit = conn.execute(
            select(AuditEntry.before, AuditEntry.after).where(
                AuditEntry.action == "settings.timezone"
            )
        ).one()
    assert set(old) == {"voided"}
    assert parlay == "push"  # both legs voided: the whole stake comes back
    assert set(pool_status) == {"refunded"}
    assert new and all(k.endswith(f"@{CHICAGO}") and s == "open" for k, s in new)
    assert {k for _, _, k in posts if k.startswith("market_voided")} == set()  # one summary
    summary = [p for c, p, _ in posts if p.get("kind") == "timezone_changed"]
    assert summary == [
        {
            "kind": "timezone_changed",
            "from": "America/New_York",
            "to": CHICAGO,
            "markets": 4,
            "bets": 5,
            "pools": 2,
        }
    ]
    assert sum(1 for c, p, _ in posts if c == "bet_results" and p.get("result") == "void") >= 4
    assert audit.before == {"timezone": "America/New_York"}
    assert audit.after["timezone"] == CHICAGO
    assert account(w, p1)[0] == account(w, p2)[0] == 100_000  # every stake and buy-in back
    assert timezone.change(w.engine, w.clock, None, CHICAGO) is None  # already set


def test_decided_markets_settle_instead_of_voiding(world: World) -> None:
    w = world
    p = player(w, 1)
    decided = weight_market(w, -5, day=date(2026, 10, 5))  # Oct 5 -> 6
    later = weight_market(w, -5, day=date(2026, 10, 7))
    bet(w, p, decided, "over")
    bet(w, p, later, "under")
    w.clock.set(local(2026, 10, 6, 13, 30))
    sync_sim(w.engine, w.clock)
    markets.lock_due(w.engine, w.clock.now())
    result = timezone.change(w.engine, w.clock, None, CHICAGO)
    assert result is not None and result.settled == 1 and result.voided_markets == 1
    with w.engine.connect() as conn:
        rows = dict(
            conn.execute(
                select(Market.id, Market.status).where(Market.id.in_((decided, later)))
            ).all()
        )
    assert rows == {decided: "settled", later: "voided"}


def test_no_redrop_once_todays_lock_has_passed(world: World) -> None:
    w = world
    w.clock.set(local(2026, 10, 5, 23, 30))  # 22:30 in Chicago: after the 22:00 lock
    assert not _impact(w).redrop_daily
    result = timezone.change(w.engine, w.clock, None, CHICAGO)
    assert result is not None and result.redropped == 0
    with w.engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(Market)).scalar_one() == 0


def test_weekly_and_monthly_are_not_redropped(world: World) -> None:
    w = world
    w.clock.set(local(2026, 10, 5, 12, 30))
    with w.engine.connect() as conn:
        config = instance.read(conn)
    assert config is not None
    markets.drop(w.engine, w.clock, config, Timeframe.DAILY, date(2026, 10, 5))
    timezone.change(w.engine, w.clock, None, CHICAGO)
    with w.engine.connect() as conn:
        frames = set(
            conn.execute(select(Market.timeframe).where(Market.status == "open")).scalars()
        )
    assert frames == {"daily"}


def test_nothing_open_and_unknown_zone(world: World) -> None:
    impact = _impact(world)
    assert impact.nothing_open and impact.bets == 0
    with pytest.raises(timezone.TimezoneError, match="time zone such as"):
        _impact(world, "Mars/Olympus")
    with pytest.raises(timezone.TimezoneError):
        timezone.change(world.engine, world.clock, None, "")


def test_worker_picks_up_the_new_zone(world: World) -> None:
    w = world
    config = instance.load(w.engine, w.clock, w.settings)
    jobs = domain_jobs(w.settings, config)
    timezone.change(w.engine, w.clock, None, CHICAGO)
    latest, new_jobs = reload_if_changed(w.engine, w.clock, w.settings, config, jobs)
    assert latest.timezone == CHICAGO and new_jobs is not jobs


def test_cli(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from app.cli import main

    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("DATA_DIR", str(world.settings.data_dir))
    p = player(world, 1)
    bet(world, p, weight_market(world, -5, day=date(2026, 10, 7)), "over")
    assert main(["settings", "timezone", "Nowhere/Land"]) == 1
    assert "refused" in capsys.readouterr().out
    assert main(["settings", "timezone", CHICAGO]) == 1
    out = capsys.readouterr().out
    assert "pushes and refunds 1 market(s) (1 bet(s)" in out and "--yes" in out
    assert main(["settings", "timezone", CHICAGO, "--yes"]) == 0
    assert "time zone is now America/Chicago" in capsys.readouterr().out
    assert main(["settings", "timezone", CHICAGO]) == 0
    assert "already" in capsys.readouterr().out

    assert main(["settings", "public-url", "https://picks.example.org/"]) == 0
    assert "https://picks.example.org (from admin)" in capsys.readouterr().out
    assert main(["settings", "public-url", "https://picks.example.org/x"]) == 1
    assert main(["settings", "public-url", "--clear"]) == 0
    assert "(from .env)" in capsys.readouterr().out
