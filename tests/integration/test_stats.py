import json
import re
from datetime import date, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import Engine

from app.core.clock import SimClock
from app.domain.lines import fit_weight
from app.providers.base import WeighIn
from app.services import stats
from app.services.observations import canonical_weigh_ins
from app.services.sync import run_sync
from tests.integration.test_admin_web import clients  # noqa: F401 - fixture
from tests.integration.test_sync import ScriptedProvider
from tests.integration.world import NY, World, local

AS_OF = date(2026, 10, 5)
METRICS = ("steps", "active_minutes", "intensity_minutes", "kcal", "workouts")


def test_series_come_from_the_canonical_weigh_ins_and_the_engine_fit(world: World) -> None:
    with world.engine.connect() as conn:
        data = stats.build(conn, AS_OF, 30, "lb", METRICS)
        canonical = canonical_weigh_ins(conn, AS_OF - timedelta(days=83), AS_OF)
    in_range = [c for c in canonical if c.local_date >= AS_OF - timedelta(days=29)]
    assert [w["d"] for w in data["weigh_ins"]] == [c.local_date.isoformat() for c in in_range]
    assert {w["s"] for w in data["weigh_ins"]} <= {"scale", "manual"}
    fit = fit_weight([((c.local_date - AS_OF).days, c.value / 10) for c in canonical], "lb")
    assert data["trend"][-1]["y"] == round(fit.a, 2)
    assert abs(data["trend"][-1]["hi"] - data["trend"][-1]["y"] - fit.sigma) < 0.02
    assert len(data["projection"]) == 7
    assert data["projection"][-1]["y"] == round(fit.a + 7 * fit.b, 2)
    widths = [p["hi"] - p["lo"] for p in data["projection"]]
    assert widths == sorted(widths)  # uncertainty grows with the horizon
    assert data["labels"][0] == (AS_OF - timedelta(days=29)).isoformat()
    assert data["labels"][-1] == (AS_OF + timedelta(days=7)).isoformat()
    assert [m["metric"] for m in data["metrics"]] == list(METRICS)
    steps = data["metrics"][0]
    assert len(steps["values"]) == 30 and steps["avg7"][-1] is not None
    strip = data["strip"]
    assert strip["current"] == in_range[-1].value / 10 and strip["streak"] >= 1
    assert data["provisional"] is False


def test_empty_and_cold_start(migrated_engine: Engine) -> None:
    with migrated_engine.connect() as conn:
        empty = stats.build(conn, AS_OF, 14, "lb", ("steps",))
    assert empty["weigh_ins"] == [] and empty["trend"] == [] and empty["projection"] == []
    assert empty["strip"] == {"current": None, "change_7d": None, "to_goal": None, "streak": 0}
    provider = ScriptedProvider()
    provider.weigh_ins = [WeighIn("w1", local(2026, 10, 5, 7), 90_000, "manual")]
    run_sync(migrated_engine, SimClock(local(2026, 10, 5, 12)), provider, tz=NY, unit="lb")
    with migrated_engine.connect() as conn:
        cold = stats.build(conn, AS_OF, 14, "lb", ())
    assert cold["provisional"] is True and cold["weigh_ins"][0]["s"] == "manual"
    assert all(t["y"] == cold["trend"][-1]["y"] for t in cold["trend"])  # slope 0
    assert cold["strip"]["streak"] == 1


def test_unknown_range_falls_back_to_30_days(world: World) -> None:
    with world.engine.connect() as conn:
        assert stats.build(conn, AS_OF, 1000, "lb", ())["days"] == 30


def test_stats_page_renders_the_data(clients: tuple[TestClient, TestClient, World]) -> None:  # noqa: F811
    admin_c, player_c, _w = clients
    page = player_c.get("/stats?days=14").text
    assert 'data-testid="stat-strip"' in page and 'id="weight-chart"' in page
    assert "/static/vendor/chart-4.5.1.umd.min.js" in page and "/static/stats.js" in page
    match = re.search(r'<script type="application/json" id="stats-data">(.*?)</script>', page, re.S)
    assert match and json.loads(match.group(1))["days"] == 14
    assert player_c.get("/static/stats.js").status_code == 200
    admin_c.__exit__(None, None, None)
    player_c.__exit__(None, None, None)
