"""Staged restore and factory reset with the worker handshake (BUILD_PLAN §1.5, §1.6)."""

from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.cli import main
from app.core.clock import SystemClock
from app.core.db import make_engine
from app.core.migrations import upgrade_to_head
from app.models import Account, AuditEntry, User
from app.services import backups, leaderboard, ledger, maintenance
from app.web.main import create_app
from tests.integration import web
from tests.integration.test_bets_settlement import player
from tests.integration.world import World, create_admin


def standings(w: World) -> list[tuple[str, int, int]]:
    with w.engine.connect() as conn:
        season = ledger.active_season_id(conn)
        return [
            (s.display_name, s.pnl_cents, s.balance_cents)
            for s in leaderboard.standings(conn, season)
        ]


def reopen(w: World) -> None:
    """After a swap: migrate the (possibly restored) file and point the world at it."""
    w.engine.dispose()
    upgrade_to_head(w.engine, Path(w.settings.data_dir) / ".migrate.lock")


def test_restore_round_trip_keeps_the_old_database(world: World) -> None:
    w = world
    player(w, 1)
    uploads = Path(w.settings.data_dir) / "uploads"
    uploads.mkdir()
    (uploads / "logo.png").write_bytes(b"backup logo")
    path = backups.create(w.engine, w.settings, w.clock, label="drill")
    before = standings(w)
    player(w, 2)  # changes after the backup
    (uploads / "logo.png").write_bytes(b"newer logo")
    assert standings(w) != before
    maintenance.stage_restore(w.engine, w.settings, w.clock, None, path)
    assert maintenance.pending_action(w.settings)["kind"] == "restore"  # type: ignore[index]
    with pytest.raises(maintenance.MaintenanceError, match="already waiting"):
        maintenance.stage_reset(
            w.engine, w.settings, w.clock, None, confirm="WeightPicks", wipe_garmin=False
        )
    assert maintenance.worker_should_stop(w.settings) is True  # the worker acknowledges
    w.engine.dispose()
    result = maintenance.apply(w.settings, wait_seconds=1, poll=0.01)
    assert result.status == "restored" and result.detail.startswith("pre-restore-")
    assert maintenance.pending_action(w.settings) is None
    reopen(w)
    assert standings(w) == before
    assert (uploads / "logo.png").read_bytes() == b"backup logo"
    kept = Path(w.settings.data_dir) / "backups" / result.detail
    old = make_engine(f"sqlite:///{kept}")
    with old.connect() as conn:
        assert conn.execute(select(func.count()).select_from(User)).scalar_one() == 2
    old.dispose()
    assert (
        Path(w.settings.data_dir) / "backups" / result.detail.replace(".db", "-uploads")
    ).is_dir()
    note = maintenance.record_done(w.engine, w.settings, SystemClock())
    assert note is not None and note["source"] == path.name
    with w.engine.connect() as conn:
        assert (
            "maintenance.restore_applied" in conn.execute(select(AuditEntry.action)).scalars().all()
        )
        assert ledger.verify(conn).ok
    assert maintenance.record_done(w.engine, w.settings, SystemClock()) is None  # once


def test_reset_archives_and_starts_fresh(world: World) -> None:
    w = world
    player(w, 1)
    with pytest.raises(maintenance.MaintenanceError, match="instance name"):
        maintenance.stage_reset(
            w.engine, w.settings, w.clock, None, confirm="nope", wipe_garmin=False
        )
    maintenance.stage_reset(
        w.engine, w.settings, w.clock, None, confirm="WeightPicks", wipe_garmin=False
    )
    maintenance.worker_should_stop(w.settings)
    w.engine.dispose()
    result = maintenance.apply(w.settings, wait_seconds=1, poll=0.01)
    assert result.status == "reset"
    reopen(w)
    with w.engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(User)).scalar_one() == 0
        assert conn.execute(select(func.count()).select_from(Account)).scalar_one() == 0
    assert (Path(w.settings.data_dir) / "backups" / result.detail).is_file()


def test_apply_waits_for_the_worker_and_changes_nothing_on_timeout(world: World) -> None:
    w = world
    from app.worker.jobs.heartbeat import HeartbeatJob
    from app.worker.registry import run_due

    run_due([HeartbeatJob()], w.engine, SystemClock())  # a live worker heartbeat
    maintenance.stage_reset(
        w.engine, w.settings, w.clock, None, confirm="WeightPicks", wipe_garmin=False
    )
    users_before = _count_users(w)
    result = maintenance.apply(w.settings, wait_seconds=0.05, poll=0.01)
    assert result.status == "timeout"
    assert maintenance.pending_action(w.settings) is not None and _count_users(w) == users_before
    # A worker that restarts while staged acknowledges without touching the database.
    assert maintenance.wait_until_clear(w.settings, poll=0.01, timeout=0.05) is False
    assert maintenance.apply(w.settings, wait_seconds=1, poll=0.01).status == "reset"
    assert maintenance.wait_until_clear(w.settings, poll=0.01, timeout=0.05) is True
    assert maintenance.apply(w.settings).status == "none"


def _count_users(w: World) -> int:
    with w.engine.connect() as conn:
        return int(conn.execute(select(func.count()).select_from(User)).scalar_one())


def test_garmin_wipe_only_when_asked(world: World, tmp_path: Path) -> None:
    w = world
    home = tmp_path / "garmin"
    (home / ".GarminDb").mkdir(parents=True)
    (home / ".GarminDb" / "garmin_tokens.json").write_text("{}")
    (home / "DBs").mkdir()
    (home / "DBs" / "garmin.db").write_text("x")
    settings = w.settings.model_copy(update={"garmin_home": home})
    maintenance.stage_reset(
        w.engine, settings, w.clock, None, confirm="WeightPicks", wipe_garmin=False
    )
    maintenance.worker_should_stop(settings)
    assert (home / "DBs" / "garmin.db").exists()
    maintenance.cancel(settings)
    maintenance.stage_reset(
        w.engine, settings, w.clock, None, confirm="WeightPicks", wipe_garmin=True
    )
    maintenance.worker_should_stop(settings)
    assert list(home.iterdir()) == []


def test_bad_backups_are_refused_at_staging(world: World) -> None:
    w = world
    bad = Path(w.settings.data_dir) / "backups"
    bad.mkdir()
    (bad / "wp-bad.tar.gz").write_bytes(b"junk")
    with pytest.raises(maintenance.MaintenanceError, match="failed checks"):
        maintenance.stage_restore(w.engine, w.settings, w.clock, None, bad / "wp-bad.tar.gz")
    assert maintenance.pending_action(w.settings) is None


def test_web_answers_503_while_staged(world: World) -> None:
    w = world
    c = web.client(create_app(w.settings, domain_clock=w.clock))
    with c:
        create_admin(
            w.engine,
            SystemClock(),
            email="a@example.invalid",
            display_name="A",
            password=web.PASSWORD,
        )
        maintenance.stage_reset(
            w.engine, w.settings, w.clock, None, confirm="WeightPicks", wipe_garmin=False
        )
        r = c.get("/login")
        assert r.status_code == 503 and "Back in a minute" in r.text
        assert c.get("/healthz").status_code == 200
        assert c.get("/brand/favicon.png").status_code == 200


def test_cli_stage_status_cancel(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    w = world
    for key, value in {
        "APP_ENV": "dev",
        "DATA_DIR": str(w.settings.data_dir),
        "LOG_FORMAT": "console",
    }.items():
        monkeypatch.setenv(key, value)
    path = backups.create(w.engine, w.settings, w.clock)
    assert main(["maintenance", "restore", "nope.tar.gz"]) == 1
    assert main(["maintenance", "restore", path.name]) == 0
    assert "restart the containers" in capsys.readouterr().out
    assert main(["maintenance", "status"]) == 0
    assert "'kind': 'restore'" in capsys.readouterr().out
    assert main(["maintenance", "reset", "--confirm", "WeightPicks"]) == 1  # busy
    assert main(["maintenance", "cancel"]) == 0
    assert main(["maintenance", "status"]) == 0
    assert "nothing staged" in capsys.readouterr().out
