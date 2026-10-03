from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.cli import main
from app.services import reconcile
from app.services.observations import canonical_weigh_ins
from tests.integration.world import World

NY = ZoneInfo("America/New_York")


def test_reconcile_lines_up_raw_data_with_what_the_engine_uses(world: World) -> None:
    with world.engine.connect() as conn:
        reports = reconcile.build(conn, date(2026, 10, 4), 7)
        canonical = {
            c.local_date: c.value
            for c in canonical_weigh_ins(conn, date(2026, 9, 28), date(2026, 10, 4))
        }
        text = reconcile.render(reports, conn, NY, "lb")
    assert reports[0].day == date(2026, 9, 28) and len(reports) == 7
    assert {r.day: r.canonical for r in reports if r.canonical} == canonical
    assert all(r.totals.keys() >= {"steps", "kcal"} for r in reports)
    assert "in window" in text and "steps" in text
    # A day whose canonical weigh-in exists lists that same value among its raw entries.
    day = next(r for r in reports if r.canonical)
    assert day.canonical in [w[1] for w in day.weigh_ins]


def test_reconcile_cli_on_an_empty_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for key, value in {
        "APP_ENV": "dev",
        "DATA_PROVIDER": "garmindb",
        "DATA_DIR": str(tmp_path),
    }.items():
        monkeypatch.setenv(key, value)
    assert main(["migrate"]) == 0
    assert main(["report", "reconcile", "--days", "2"]) == 0
    out = capsys.readouterr().out
    assert "no successful sync yet" in out and out.count("canonical weigh-in: -") == 2
