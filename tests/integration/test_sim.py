import os
import time
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select

from app.cli import main
from app.core.clock import SystemClock
from app.core.config import Settings
from app.core.db import make_engine
from app.models import Heartbeat, Observation
from app.services import sim
from app.services.instance import InstanceConfig
from app.services.observations import canonical_weigh_ins
from app.web.main import create_app
from app.worker.jobs import INFRA_JOBS, domain_jobs
from app.worker.main import tick


@pytest.fixture
def dev(settings: Settings) -> Settings:
    return settings.model_copy(update={"data_provider": "simulated"})


def _observations(engine: Engine) -> int:
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(Observation)).scalar_one()


def test_persisted_sim_clock_is_frozen_until_advanced(
    migrated_engine: Engine, dev: Settings
) -> None:
    clock = sim.app_clock(dev, migrated_engine)
    assert isinstance(clock, sim.PersistedSimClock)
    first = clock.now()
    time.sleep(0.01)
    assert clock.now() == first
    result = sim.advance(migrated_engine, dev, timedelta(hours=3))
    assert clock.now() == first + timedelta(hours=3) == result.end
    assert result.ticks == 180
    assert result.jobs_run["garmin_sync"] >= 2


def test_production_uses_real_time(migrated_engine: Engine, tmp_path: Path) -> None:
    prod = Settings(app_env="production", app_secret_key="x" * 40, data_dir=tmp_path)
    assert isinstance(sim.app_clock(prod, migrated_engine), SystemClock)


def test_advance_set_and_reseed_rules(migrated_engine: Engine, dev: Settings) -> None:
    with pytest.raises(ValueError, match="forward"):
        sim.advance(migrated_engine, dev, timedelta(0))
    state = sim.load_state(migrated_engine, dev)
    with pytest.raises(ValueError, match="forward"):
        sim.set_now(migrated_engine, dev, state.sim_now - timedelta(minutes=1))
    sim.set_now(migrated_engine, dev, state.sim_now + timedelta(days=2))
    assert sim.load_state(migrated_engine, dev).sim_now == state.sim_now + timedelta(days=2)
    with pytest.raises(ValueError, match="preset"):
        sim.reseed(migrated_engine, dev, preset="marathon", seed=1)


def test_sync_job_reuses_provider_until_reseed(
    migrated_engine: Engine, dev: Settings, instance_config: InstanceConfig
) -> None:
    job = next(j for j in domain_jobs(dev, instance_config) if j.name == "garmin_sync")
    sim.advance(migrated_engine, dev, timedelta(hours=1))
    clock = sim.app_clock(dev, migrated_engine)
    from app.worker.registry import JobContext

    ctx = JobContext(migrated_engine, clock, clock.now(), "k")
    job.run(ctx)
    first = job._provider  # type: ignore[attr-defined]
    job.run(ctx)
    assert job._provider is first  # type: ignore[attr-defined]
    sim.reseed(migrated_engine, dev, preset="plateau", seed=5)
    job.run(ctx)
    assert job._provider is not first  # type: ignore[attr-defined]


def test_reseed_keeps_ingested_history(migrated_engine: Engine, dev: Settings) -> None:
    sim.advance(migrated_engine, dev, timedelta(days=5))
    state = sim.load_state(migrated_engine, dev)
    with migrated_engine.connect() as conn:
        before = canonical_weigh_ins(conn, state.anchor_date, state.sim_now.date())
    sim.reseed(migrated_engine, dev, preset="chaotic", seed=999)
    sim.advance(migrated_engine, dev, timedelta(days=3))
    with migrated_engine.connect() as conn:
        after = canonical_weigh_ins(conn, state.anchor_date, state.sim_now.date())
    # Days already complete before the reseed keep their weigh-ins.
    settled = [c for c in before if c.local_date < state.sim_now.date()]
    assert settled == [c for c in after if c.local_date < state.sim_now.date()]
    assert sim.load_state(migrated_engine, dev).preset == "chaotic"


def test_heartbeat_uses_real_time_while_sim_clock_is_frozen(
    migrated_engine: Engine, dev: Settings, instance_config: InstanceConfig
) -> None:
    sim_clock = sim.app_clock(dev, migrated_engine)
    sim_now = sim_clock.now()
    sim.set_now(migrated_engine, dev, sim_now + timedelta(days=30))
    ran = tick(
        migrated_engine,
        infra=INFRA_JOBS,
        domain=domain_jobs(dev, instance_config),
        system_clock=SystemClock(),
        domain_clock=sim_clock,
        seen={},
    )
    assert "heartbeat" in ran and "garmin_sync" in ran
    with migrated_engine.connect() as conn:
        beat = conn.execute(select(Heartbeat.beat_at)).scalar_one()
    assert abs(beat - SystemClock().now()) < timedelta(minutes=1)
    assert sim_clock.now() == sim_now + timedelta(days=30)


def test_dev_routes_only_exist_in_dev(
    settings: Settings, dev: Settings, migrated_engine: Engine, tmp_path: Path
) -> None:
    prod = Settings(app_env="production", app_secret_key="x" * 40, data_dir=tmp_path)
    with TestClient(create_app(prod)) as client:
        assert client.get("/dev/clock").status_code == 404
    with TestClient(create_app(settings)) as client:  # dev, but garmindb provider
        assert client.get("/dev/clock").status_code == 404


def test_dev_clock_page_and_forms(dev: Settings, migrated_engine: Engine) -> None:
    with TestClient(create_app(dev)) as client:
        page = client.get("/dev/clock")
        assert page.status_code == 200
        assert "Simulation clock" in page.text
        assert page.headers["X-Robots-Tag"] == "noindex, nofollow"
        start = sim.load_state(migrated_engine, dev).sim_now

        moved = client.post(
            "/dev/clock/advance",
            content="days=1&hours=6",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert moved.status_code == 303
        assert sim.load_state(migrated_engine, dev).sim_now == start + timedelta(days=1, hours=6)
        assert _observations(migrated_engine) > 0

        bad = client.post(
            "/dev/clock/reseed",
            content="preset=%3Cscript%3E&seed=1",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert bad.status_code == 200  # followed the redirect back to the page
        assert "unknown preset" in bad.text
        assert "<script>" not in bad.text  # escaped


def test_sim_cli_refuses_without_dev_simulated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    assert main(["sim", "status"]) == 2  # DATA_PROVIDER defaults to garmindb


@pytest.mark.perf
def test_sim_advance_90_days_under_10_seconds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """STAGE03 measure of success."""
    for key, value in {
        "APP_ENV": "dev",
        "DATA_PROVIDER": "simulated",
        "DATA_DIR": str(tmp_path),
        "LOG_FORMAT": "console",
        "LOG_LEVEL": "WARNING",
    }.items():
        monkeypatch.setenv(key, value)
    assert main(["migrate"]) == 0
    started = time.perf_counter()
    assert main(["sim", "advance", "--days", "90"]) == 0
    elapsed = time.perf_counter() - started
    out = capsys.readouterr().out
    # 10 s on the dev machine; CI sets PERF_BUDGET_SCALE=2 for slower shared runners (D-028).
    budget = 10 * float(os.environ.get("PERF_BUDGET_SCALE", "1"))
    assert elapsed < budget, out

    engine = make_engine(f"sqlite:///{tmp_path / 'app.db'}")
    settings = Settings()
    state = sim.load_state(engine, settings)
    with engine.connect() as conn:
        canonical = canonical_weigh_ins(conn, state.anchor_date, state.sim_now.date())
    engine.dispose()
    manual = sum(c.source == "manual" for c in canonical)
    assert 65 <= len(canonical) <= 91  # ~90 days minus ~12% skips
    assert manual > 0
    print(f"\n90 days: {elapsed:.2f}s, {len(canonical)} canonical, {manual} manual")
