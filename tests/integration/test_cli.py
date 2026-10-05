import socket
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine, inspect
from sqlalchemy.dialects.sqlite import insert

from app.cli import main
from app.core.clock import SystemClock
from app.core.db import immediate, make_engine
from app.models import Heartbeat


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LOG_FORMAT", "console")
    return tmp_path


def _beat(engine: Engine, age: timedelta) -> None:
    with immediate(engine) as conn:
        beat_at = SystemClock().now() - age
        conn.execute(insert(Heartbeat).values(component="worker", beat_at=beat_at))


def test_migrate_creates_schema_under_data_dir(cli_env: Path) -> None:
    assert main(["migrate"]) == 0
    assert main(["migrate"]) == 0
    engine = make_engine(f"sqlite:///{cli_env / 'app.db'}")
    assert "job_runs" in inspect(engine).get_table_names()
    assert (cli_env / ".migrate.lock").exists()
    engine.dispose()


FRESH, STALE = timedelta(seconds=30), timedelta(minutes=4)


@pytest.mark.parametrize(("age", "expected"), [(FRESH, 0), (STALE, 1)])
def test_worker_health_checks_heartbeat_age(cli_env: Path, age: timedelta, expected: int) -> None:
    main(["migrate"])
    engine = make_engine(f"sqlite:///{cli_env / 'app.db'}")
    _beat(engine, age)
    engine.dispose()
    assert main(["health", "--worker"]) == expected


def test_worker_health_fails_without_heartbeat_or_schema(cli_env: Path) -> None:
    assert main(["health", "--worker"]) == 1
    main(["migrate"])
    assert main(["health", "--worker"]) == 1


def _closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_web_health_fails_when_nothing_listens(
    cli_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Use a port that was just free so a running dev stack on :8000 can't interfere.
    monkeypatch.setattr("app.cli.WEB_HEALTH_URL", f"http://127.0.0.1:{_closed_port()}/healthz")
    assert main(["health", "--web"]) == 1


def test_ai_commands_without_setup_or_token(
    cli_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["migrate"]) == 0
    assert main(["ai", "quota"]) == 0
    assert "neurons used today (UTC): 0 of 5,000" in capsys.readouterr().out
    assert main(["ai", "run", "daily"]) == 0  # no settings row: AI is simply off
    assert "ai daily: off" in capsys.readouterr().out
