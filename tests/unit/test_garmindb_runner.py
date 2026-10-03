import stat
import sys
from datetime import date
from pathlib import Path

import pytest

from app.providers import garmindb_runner
from app.providers.garmindb_runner import GarminDBRunner, GarminRunError, scrub

FAKE_CLI = """import os, sys
mode = open(os.path.join(os.environ["HOME"], "mode")).read().strip()
if mode == "login_failed":
    print("Processing profile data: {'userName': 'me@example.invalid', 'weight': 99.1}")
    print("ERROR login failed: 401 Unauthorized"); print("Failed to login!"); sys.exit(0)
if mode == "crash":
    print("Traceback (most recent call last):"); sys.exit(1)
if mode == "exit":
    print("token abcdefghijklmnopqrstuvwxyz0123 me@example.invalid boom"); sys.exit(3)
if mode == "slow":
    import time; time.sleep(5)
line = " ".join(sys.argv[1:]) + "|" + os.environ["TZ"] + "\\n"
open(os.path.join(os.environ["HOME"], "ran"), "a").write(line)
"""


@pytest.fixture
def runner(tmp_path: Path) -> GarminDBRunner:
    bin_dir = tmp_path / "venv" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "garmindb_cli.py").write_text(FAKE_CLI)
    python = bin_dir / "python"
    python.symlink_to(sys.executable)
    (tmp_path / "home").mkdir()
    (tmp_path / "home" / "mode").write_text("ok")
    return GarminDBRunner(tmp_path / "home", python, "America/Chicago", timeout=2)


def mode(r: GarminDBRunner, value: str) -> None:
    (r.home / "mode").write_text(value)


def test_runs_garmindb_then_the_recent_fetch(
    runner: GarminDBRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    recent = runner.home / "recent.py"
    recent.write_text(
        "import os, sys\n"
        "ran = os.path.join(os.environ['HOME'], 'ran')\n"
        "open(ran, 'a').write('recent ' + sys.argv[1] + '\\n')\n"
    )
    recent.chmod(recent.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(GarminDBRunner, "recent_script", property(lambda _self: recent))
    runner(date(2026, 10, 3))
    ran = (runner.home / "ran").read_text().splitlines()
    assert ran == [
        "--all --download --import --analyze --latest|America/Chicago",
        "recent 2026-10-03",
    ]


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("login_failed", "rerun garmin-login"),  # GarminDB exits 0 on a failed login
        ("crash", "GarminDB crashed"),
        ("exit", "GarminDB exited 3"),
        ("slow", "GarminDB timed out"),
    ],
)
def test_failures_are_loud_and_scrubbed(runner: GarminDBRunner, value: str, message: str) -> None:
    mode(runner, value)
    with pytest.raises(GarminRunError, match=message) as info:
        runner(date(2026, 10, 3))
    text = str(info.value)
    assert "example.invalid" not in text and "abcdefghijklmnop" not in text
    assert "99.1" not in text


def test_missing_program_fails_cleanly(tmp_path: Path) -> None:
    r = GarminDBRunner(tmp_path, tmp_path / "nope" / "python", "UTC")
    with pytest.raises(GarminRunError, match="could not start"):
        r(date(2026, 10, 3))


def test_one_sync_at_a_time(runner: GarminDBRunner) -> None:
    assert garmindb_runner._lock.acquire(blocking=False)
    try:
        with pytest.raises(GarminRunError, match="already running"):
            runner(date(2026, 10, 3))
    finally:
        garmindb_runner._lock.release()


def test_scrub() -> None:
    raw = "me@example.invalid ticket ST-123-abc token " + "x" * 30 + " ok"
    assert scrub(raw) == "<email> ticket <ticket> token <redacted> ok"
    assert len(scrub("a " * 500)) == 300
