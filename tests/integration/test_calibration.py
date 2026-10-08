from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app import calibration
from app.cli import main

NY = ZoneInfo("America/New_York")
REPO = Path(__file__).resolve().parents[2]


def test_small_run_report_is_deterministic_and_well_formed() -> None:
    first = calibration.run("steady-loser", days=150, seeds=2, tz=NY, unit="lb")
    second = calibration.run("steady-loser", days=150, seeds=2, tz=NY, unit="lb")
    text = calibration.render_markdown(first)
    assert text == calibration.render_markdown(second)
    assert first.samples > 1000
    assert first.provisional_days > 0
    assert len(first.deciles) == 10
    assert "| Implied P(Over) | n | Mean implied | Realised | Difference | OK |" in text
    assert "**Result:" in text


def test_too_little_data_is_reported_as_insufficient(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    report = calibration.run("chaotic", days=60, seeds=1, tz=NY, unit="lb")
    assert report.status == "INSUFFICIENT DATA"
    assert "**Result: INSUFFICIENT DATA**" in calibration.render_markdown(report)
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    args = ["calibrate", "--preset", "chaotic", "--days", "60", "--seeds", "1", "--out", "-"]
    assert main(args) == 2


def test_decile_grading_rules() -> None:
    ok = calibration.Decile(low=0.5, n=500, implied=0.55, realised=0.59)
    bad = calibration.Decile(low=0.5, n=500, implied=0.55, realised=0.61)
    small = calibration.Decile(low=0.0, n=50, implied=0.05, realised=0.30)
    assert ok.ok and not bad.ok
    assert not small.graded and small.ok


def test_cli_writes_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    code = main(
        [
            "calibrate",
            "--preset",
            "plateau",
            "--days",
            "150",
            "--seeds",
            "2",
            "--out",
            str(tmp_path),
        ]
    )
    assert code in (0, 1)
    assert (tmp_path / "plateau.md").read_text().startswith("# Line-engine calibration")
    assert "wrote" in capsys.readouterr().out
    assert main(
        ["calibrate", "--preset", "plateau", "--days", "150", "--seeds", "1", "--out", "-"]
    ) in (0, 1, 2)  # 2 = insufficient data for a 1-seed run
    assert "**Result:" in capsys.readouterr().out


@pytest.mark.calibration
@pytest.mark.parametrize(
    "preset", ["steady-loser", "chaotic", "rebound", "plateau", "goal-in-30-days"]
)
def test_full_calibration_passes_and_committed_report_is_current(preset: str) -> None:
    """Every graded decile within ±5 points; docs/calibration/ is up to date."""
    report = calibration.run(preset, days=365, seeds=20, tz=NY, unit="lb")
    assert report.passed, calibration.render_markdown(report)
    committed = REPO / "docs" / "calibration" / f"{preset}.md"
    assert committed.read_text() == calibration.render_markdown(report), (
        f"regenerate with: uv run wp calibrate --preset {preset}"
    )
