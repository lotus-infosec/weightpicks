"""`docker compose run --rm garmin-login`: the one interactive Garmin Connect login (D-038).

Runs under GarminDB's own venv (/opt/garmindb), so it imports only the standard library
and garminconnect. It writes the token file GarminDB reuses, plus a GarminDB config with
no username or password in it: later syncs run on the token alone, and an expired token
fails loudly instead of silently falling back to stored credentials. Nothing secret is
printed. Rerun it whenever the admin dashboard says the Garmin login expired.
"""

import getpass
import json
import os
import re
import sys
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

CONFIG_DIR = Path(os.environ.get("GARMINDB_CONFIG_DIR", "/garmin/.GarminDb"))
TOKEN_FILE = "garmin_tokens.json"  # noqa: S105 - a file name, not a secret
CONFIG_FILE = "GarminConnectConfig.json"
HISTORY_DAYS = 120  # the first sync downloads this much history, not years of it
MONITORING_DAYS = 45
STATS = ("weight", "sleep", "rhr", "hrv", "monitoring")

Factory = Callable[..., Any]
Ask = Callable[[str], str]


def garmindb_config(today: date) -> dict[str, Any]:
    """GarminDB settings: only the data WeightPicks reads, and no credentials."""
    start = (today - timedelta(days=HISTORY_DAYS)).strftime("%m/%d/%Y")
    monitoring_start = (today - timedelta(days=MONITORING_DAYS)).strftime("%m/%d/%Y")
    return {
        "db": {"type": "sqlite"},
        "garmin": {"domain": "garmin.com"},
        "credentials": {
            "user": "",
            "secure_password": False,
            "password": "",
            "password_file": None,
        },
        "data": {
            **{f"{stat}_start_date": start for stat in STATS},
            # Monitoring downloads a FIT archive per day (~5 s each); 45 days keeps the
            # first sync well inside the 15-minute timeout.
            "monitoring_start_date": monitoring_start,
            "download_latest_activities": 25,
            "download_all_activities": 200,
        },
        "directories": {
            "relative_to_home": True,
            "base_dir": "HealthData",
            "mount_dir": "/nonexistent",
        },
        # monitoring brings the daily summaries (steps, intensity minutes, calories).
        "enabled_stats": {
            "monitoring": True,
            "steps": False,
            "itime": False,
            "sleep": False,
            "rhr": False,
            "hrv": False,
            "weight": True,
            "activities": True,
        },
        "course_views": {"steps": []},
        "modes": {},
        "activities": {"display": []},
        "settings": {"metric": True, "default_display_activities": []},
        "checkup": {"look_back_days": 30},
    }


def _write_private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(text)
    path.chmod(0o600)


BROWSER_SERVICE = "https://mobile.integration.garmin.com/gcm/ios"
BROWSER_URL = (
    "https://sso.garmin.com/portal/sso/en-US/sign-in?clientId=GCM_IOS_DARK"
    "&service=https%3A%2F%2Fmobile.integration.garmin.com%2Fgcm%2Fios"
)
TICKET = re.compile(r"ST-[A-Za-z0-9._-]+")


def _password_login(token_file: Path, factory: Factory, ask: Ask, ask_secret: Ask) -> Any:
    email = ask("Garmin Connect email: ").strip()
    password = ask_secret("Garmin Connect password (hidden): ")
    if not email or not password:
        raise SystemExit("Email and password are both required.")
    client = factory(
        email=email,
        password=password,
        prompt_mfa=lambda: ask("MFA code from Garmin: ").strip(),
    )
    del password
    client.login(str(token_file))
    return client


def _browser_login(token_file: Path, factory: Factory, ask: Ask, ask_secret: Ask) -> Any:
    """Sign in on Garmin's own page in a normal browser, then trade the one-time ticket
    it hands back for the same tokens a password login gets. Avoids the scripted SSO
    login that Garmin rate-limits (T-011)."""
    print(
        "\n1. Open this link in your browser and sign in (MFA as usual):\n"
        f"   {BROWSER_URL}\n"
        "2. The browser ends on a page that fails to load. Copy the whole address\n"
        "   from the address bar (it contains ticket=ST-...). The ticket works once and\n"
        "   only for a few minutes.\n"
    )
    pasted = ask_secret("Paste the address or ticket (hidden): ")
    match = TICKET.search(pasted)
    if not match:
        raise SystemExit("No ticket found: it starts with ST- . Sign in again for a fresh one.")
    garmin = factory()
    garmin.client._exchange_service_ticket(match.group(0), service_url=BROWSER_SERVICE)
    garmin.client.dump(str(token_file))
    client = factory()
    client.login(str(token_file))  # proves the stored token works on its own
    return client


def run(
    config_dir: Path,
    factory: Factory,
    ask: Ask = input,
    ask_secret: Ask = getpass.getpass,
    today: date | None = None,
) -> str:
    """Log in once, store the token, return the account's display name."""
    config_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    config_dir.chmod(0o700)
    token_file = config_dir / TOKEN_FILE
    if token_file.exists():
        token_file.unlink()  # a fresh login replaces whatever was there
    choice = ask("Sign in with [1] email + password or [2] a browser sign-in? [1]: ").strip()
    login = _browser_login if choice == "2" else _password_login
    client = login(token_file, factory, ask, ask_secret)
    if not token_file.is_file():
        raise SystemExit("Logged in, but Garmin did not hand back a reusable token.")
    token_file.chmod(0o600)
    config = garmindb_config(today or datetime.now(UTC).date())
    _write_private(config_dir / CONFIG_FILE, json.dumps(config, indent=2) + "\n")
    return str(getattr(client, "display_name", None) or "your account")


def main() -> int:
    try:
        from garminconnect import Garmin  # type: ignore[import-not-found,unused-ignore]
    except ImportError:
        print("garminconnect is missing: run this inside the WeightPicks image.", file=sys.stderr)
        return 2
    try:
        name = run(CONFIG_DIR, Garmin)
    except SystemExit as exc:
        print(exc, file=sys.stderr)
        return 1
    except Exception as exc:  # the message is all the user needs
        print(f"Garmin login failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"Logged in as {name}. The token is saved; the worker syncs on its own from now on.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
