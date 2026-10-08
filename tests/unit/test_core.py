import re
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine, text

from app.core.clock import SimClock, SystemClock
from app.core.config import Settings
from app.core.db import immediate

APP_DIR = Path(__file__).resolve().parents[2] / "app"


# ---- Clock -------------------------------------------------------------------


def test_simclock_advance_moves_now_exactly(clock: SimClock) -> None:
    start = clock.now()
    clock.advance(timedelta(days=7, seconds=1))
    assert clock.now() - start == timedelta(days=7, seconds=1)


def test_simclock_normalises_to_utc_and_rejects_naive() -> None:
    eastern = timezone(timedelta(hours=-4))
    sim = SimClock(datetime(2026, 10, 5, 2, 30, tzinfo=eastern))
    assert sim.now() == datetime(2026, 10, 5, 6, 30, tzinfo=UTC)
    assert sim.now().tzinfo is UTC
    with pytest.raises(ValueError, match="timezone-aware"):
        SimClock(datetime(2026, 10, 5))  # noqa: DTZ001 - deliberately naive
    with pytest.raises(ValueError, match="timezone-aware"):
        sim.set(datetime(2026, 10, 6))  # noqa: DTZ001 - deliberately naive


def test_system_clock_is_aware_utc() -> None:
    assert SystemClock().now().tzinfo is UTC


def test_no_direct_time_calls_outside_clock() -> None:
    forbidden = re.compile(r"datetime\.(now|utcnow|today)\(|\btime\.time\(|\bdate\.today\(")
    offenders = [
        f"{path.relative_to(APP_DIR.parent)}:{n}"
        for path in APP_DIR.rglob("*.py")
        if path.name != "clock.py" or path.parent.name != "core"
        # Interactive one-off under GarminDB's venv; it cannot import the app.
        if path.name != "garmin_login.py"
        for n, line in enumerate(path.read_text().splitlines(), 1)
        if forbidden.search(line)
    ]
    assert offenders == [], "use the Clock abstraction (AGENTS.md rule 7)"


# ---- Config ------------------------------------------------------------------


def test_production_requires_a_long_secret_key(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="APP_SECRET_KEY"):
        Settings(app_env="production", data_dir=tmp_path)
    ok = Settings(app_env="production", app_secret_key="x" * 32, data_dir=tmp_path)
    assert ok.db_url == f"sqlite:///{tmp_path / 'app.db'}"


def test_settings_read_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("DATA_PROVIDER", "simulated")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///elsewhere.db")
    s = Settings()
    assert s.is_dev
    assert s.data_provider == "simulated"
    assert s.db_url == "sqlite:///elsewhere.db"


def test_real_data_never_runs_on_the_sim_clock() -> None:
    assert Settings(app_env="dev", data_provider="simulated").sim_clock
    assert not Settings(app_env="dev", data_provider="garmindb").sim_clock
    assert not Settings(app_secret_key="k" * 64, data_provider="simulated").sim_clock


def test_time_zone_and_unit(monkeypatch: pytest.MonkeyPatch) -> None:
    s = Settings(app_env="dev")
    assert (s.wp_timezone, s.wp_unit, s.tz.key) == ("America/New_York", "lb", "America/New_York")
    monkeypatch.setenv("WP_TIMEZONE", "Europe/Berlin")
    monkeypatch.setenv("WP_UNIT", "kg")
    assert (Settings(app_env="dev").tz.key, Settings(app_env="dev").wp_unit) == (
        "Europe/Berlin",
        "kg",
    )
    with pytest.raises(ValidationError, match="unknown time zone"):
        Settings(app_env="dev", wp_timezone="Mars/Olympus")


def test_secret_key_is_not_shown_in_repr() -> None:
    s = Settings(app_env="dev", app_secret_key="super-secret-value-123")
    assert "super-secret-value-123" not in repr(s)


# ---- Database ----------------------------------------------------------------


def _pragmas(engine: Engine) -> dict[str, object]:
    with engine.connect() as conn:
        return {
            name: conn.exec_driver_sql(f"PRAGMA {name}").scalar()
            for name in ("journal_mode", "synchronous", "foreign_keys", "busy_timeout")
        }


def test_pragmas_applied_on_every_new_connection(engine: Engine) -> None:
    expected = {"journal_mode": "wal", "synchronous": 1, "foreign_keys": 1, "busy_timeout": 10000}
    assert _pragmas(engine) == expected
    engine.dispose()  # force brand-new DBAPI connections
    assert _pragmas(engine) == expected


def test_immediate_commits_and_rolls_back(engine: Engine) -> None:
    with immediate(engine) as conn:
        conn.execute(text("CREATE TABLE t (x INTEGER)"))
        conn.execute(text("INSERT INTO t VALUES (1)"))
    with pytest.raises(RuntimeError), immediate(engine) as conn:
        conn.execute(text("INSERT INTO t VALUES (2)"))
        raise RuntimeError("boom")
    with engine.connect() as conn:
        assert conn.execute(text("SELECT x FROM t")).scalars().all() == [1]


def test_immediate_takes_the_write_lock_up_front(engine: Engine) -> None:
    with immediate(engine) as conn:
        conn.execute(text("CREATE TABLE t (x INTEGER)"))
    with immediate(engine) as conn:
        # Before any write, this transaction must already hold RESERVED.
        other = engine.raw_connection()
        try:
            other.execute("PRAGMA busy_timeout=0")
            with pytest.raises(Exception, match="locked"):
                other.execute("BEGIN IMMEDIATE")
        finally:
            other.close()
        conn.execute(text("SELECT 1"))
