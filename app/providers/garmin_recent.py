"""Fetch weigh-ins, the watch's last upload and fresh daily summaries with the saved
Garmin token.

GarminDB is not used for weight: its download range stops at yesterday (a weigh-in
made this morning would arrive tomorrow), and it decides where to resume from its own
weight table, so a subject who rarely weighs in made it re-download every day of
history on every sync. This runs right after GarminDB, under GarminDB's venv (standard
library + garminconnect only), and writes the files the provider reads.

Daily totals are only as complete as the watch's last upload (an iPhone doesn't sync
in the background, issue #57). So this asks Garmin when the watch last uploaded, then
re-downloads any of the last `DAYS` daily summaries saved before that upload (GarminDB
itself only refreshes 3 days). The upload time is written last: the provider trusts a
summary file only if it is at least as new as the upload.

The admin's Sync now passes a day count (30): the same refresh over a month, plus any
workout in that month GarminDB's "latest activities" download missed (issue #61).
"""

import json
import os
import sys
import time
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

HOME = Path(os.environ.get("HOME", "/garmin"))
TOKEN_FILE = HOME / ".GarminDb" / "garmin_tokens.json"
DATA = HOME / "HealthData"
OUT_FILE = DATA / "Weight" / "weight_recent.json"
WATCH_FILE = DATA / "watch_sync.json"
WEIGHT_DAYS = 120  # one request, ~0.1 s; the line engine reads at most 84 days
DAYS = 7  # daily summaries refreshed after a watch upload
PAUSE_SECONDS = 0.5  # between summary requests, like GarminDB's own pacing
WEIGHT_URL = "/weight-service/weight/dateRange"
SUMMARY_URL = "/usersummary-service/usersummary/daily/"


def fetch(client: Any, today: date) -> dict[str, Any]:
    start = today - timedelta(days=WEIGHT_DAYS)
    data: dict[str, Any] = client.connectapi(
        WEIGHT_URL, params={"startDate": start.isoformat(), "endDate": today.isoformat()}
    )
    if not isinstance(data, dict) or not isinstance(data.get("dateWeightList"), list):
        raise ValueError("unexpected weight response shape")
    return data


def last_upload(client: Any) -> dict[str, Any]:
    """{"upload_ms": epoch ms or None, "device": name or None} for the last used device."""
    data = client.get_device_last_used()
    if not isinstance(data, dict):
        raise ValueError("unexpected device response shape")
    upload = data.get("lastUsedDeviceUploadTime")
    name = data.get("lastUsedDeviceName")
    return {
        "upload_ms": upload if isinstance(upload, int) and upload > 0 else None,
        "device": name if isinstance(name, str) else None,
    }


def summary_path(day: date) -> Path:
    return DATA / "FitFiles" / "Monitoring" / str(day.year) / f"daily_summary_{day}.json"


def stale_days(
    today: date, upload_ms: int | None, days: int, path: Callable[[date], Path] = summary_path
) -> list[date]:
    """Days in [today - days, yesterday] whose summary is missing or older than the upload."""
    if upload_ms is None:
        return []
    out = []
    for back in range(days, 0, -1):
        day = today - timedelta(days=back)
        file = path(day)
        if not file.exists() or file.stat().st_mtime * 1000 < upload_ms:
            out.append(day)
    return out


def fetch_summary(client: Any, day: date) -> dict[str, Any]:
    name = getattr(client, "display_name", None)
    if not name:
        raise ValueError("no Garmin display name")
    url = SUMMARY_URL + quote(str(name), safe="")
    data = client.connectapi(url, params={"calendarDate": str(day)})
    if not isinstance(data, dict):
        raise ValueError("unexpected summary response shape")
    return data


def save_activities(client: Any, today: date, days: int, folder: Path) -> int:
    """Save workouts from the last `days` days that have no file yet, in the shape
    GarminDB saves them (one activity-list entry per file). Returns how many."""
    start = today - timedelta(days=days)
    found = client.get_activities_by_date(start.isoformat(), today.isoformat())
    if not isinstance(found, list):
        raise ValueError("unexpected activities response shape")
    saved = 0
    for activity in found:
        activity_id = activity.get("activityId") if isinstance(activity, dict) else None
        if not isinstance(activity_id, int):
            continue
        path = folder / f"activity_{activity_id}.json"
        if not path.exists():
            write(activity, path)
            saved += 1
    return saved


def write(data: dict[str, Any], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(out)  # readers never see a half-written file


def main() -> int:
    try:
        from garminconnect import Garmin  # type: ignore[import-not-found,unused-ignore]

        client = Garmin()
        client.login(str(TOKEN_FILE))
        # The runner passes the instance's local date from the app clock.
        today = date.fromisoformat(sys.argv[1])
        days = int(sys.argv[2]) if len(sys.argv) > 2 else DAYS
        data = fetch(client, today)
        write(data, OUT_FILE)
        watch = last_upload(client)
        refreshed = stale_days(today, watch["upload_ms"], days)
        for n, day in enumerate(refreshed):
            if n:
                time.sleep(PAUSE_SECONDS)
            write(fetch_summary(client, day), summary_path(day))
        if days > DAYS:  # the deep Sync now
            save_activities(client, today, days, DATA / "FitFiles" / "Activities")
        write(watch, WATCH_FILE)
    except Exception as exc:  # the runner records only the class name
        print(f"recent fetch failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    print(f"recent weigh-ins: {len(data['dateWeightList'])}, summaries: {len(refreshed)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
