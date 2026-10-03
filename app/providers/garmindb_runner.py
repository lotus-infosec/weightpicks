"""Runs GarminDB, then the recent-weigh-ins fetch, as separate programs (BUILD_PLAN §2.3, D-038).

GarminDB prints profile data (weight, gender, account email) and exits 0 even when its
login fails, so its output is scanned for failure markers but never stored: errors
keep only a short, scrubbed reason.
"""

import re
import subprocess
import threading
from dataclasses import dataclass
from datetime import date
from pathlib import Path

TIMEOUT_SECONDS = 15 * 60
RECENT_TIMEOUT_SECONDS = 2 * 60
GARMINDB_ARGS = ("--all", "--download", "--import", "--analyze", "--latest")
LOGIN_EXPIRED = "Garmin login failed: the token expired or was revoked; rerun garmin-login"
FAILURE_MARKERS = (
    ("Failed to login", LOGIN_EXPIRED),
    ("login failed", LOGIN_EXPIRED),
    ("Missing config", "GarminDB config is missing; rerun garmin-login"),
    ("Traceback (most recent call last)", "GarminDB crashed"),
)
_SCRUB = (
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "<email>"),
    (re.compile(r"ST-[A-Za-z0-9._-]+"), "<ticket>"),
    (re.compile(r"[A-Za-z0-9_\-.=+/]{24,}"), "<redacted>"),
)
_lock = threading.Lock()


class GarminRunError(RuntimeError):
    """A sync step failed; the message is safe to store and show to the admin."""


def scrub(text: str, limit: int = 300) -> str:
    for pattern, replacement in _SCRUB:
        text = pattern.sub(replacement, text)
    return text[:limit]


def failure_reason(output: str) -> str | None:
    for marker, reason in FAILURE_MARKERS:
        if marker in output:
            return reason
    return None


@dataclass(frozen=True, slots=True)
class GarminDBRunner:
    home: Path
    python: Path
    tz_name: str
    timeout: float = TIMEOUT_SECONDS

    @property
    def cli(self) -> Path:
        return self.python.parent / "garmindb_cli.py"

    @property
    def recent_script(self) -> Path:
        return Path(__file__).with_name("garmin_recent.py")

    def _run(self, args: list[str], timeout: float, step: str) -> str:
        env = {"HOME": str(self.home), "TZ": self.tz_name, "PATH": "/usr/local/bin:/usr/bin:/bin"}
        try:
            proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
                args,
                cwd=self.home,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise GarminRunError(f"{step} timed out after {int(timeout // 60)} min") from exc
        except OSError as exc:
            raise GarminRunError(f"{step} could not start: {type(exc).__name__}") from exc
        output = proc.stdout + proc.stderr
        reason = failure_reason(output)
        if reason:
            raise GarminRunError(reason)
        if proc.returncode != 0:
            last = next((ln for ln in reversed(output.splitlines()) if ln.strip()), "")
            raise GarminRunError(f"{step} exited {proc.returncode}: {scrub(last)}")
        return output

    def __call__(self, today: date) -> None:
        if not _lock.acquire(blocking=False):
            raise GarminRunError("a Garmin sync is already running")
        try:
            self._run([str(self.python), str(self.cli), *GARMINDB_ARGS], self.timeout, "GarminDB")
            self._run(
                [str(self.python), "-P", str(self.recent_script), today.isoformat()],
                RECENT_TIMEOUT_SECONDS,
                "recent weigh-ins",
            )
        finally:
            _lock.release()
