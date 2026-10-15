"""The recent fetch (issue #57): the watch's last upload, and which daily summaries to
download again. A fake client stands in for garminconnect; no real data."""

import os
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from app.providers import garmin_recent

TODAY = date(2026, 10, 10)


class FakeClient:
    display_name = "someone/with?odd chars"

    def __init__(self, device: Any) -> None:
        self.device = device
        self.calls: list[tuple[str, dict[str, str] | None]] = []

    def get_device_last_used(self) -> Any:
        return self.device

    def connectapi(self, url: str, params: dict[str, str] | None = None) -> Any:
        self.calls.append((url, params))
        return {"calendarDate": params["calendarDate"] if params else None, "totalSteps": 1}


def test_last_upload_keeps_only_the_time_and_device() -> None:
    client = FakeClient(
        {"lastUsedDeviceUploadTime": 1791654963000, "lastUsedDeviceName": "Watch", "x": 1}
    )
    assert garmin_recent.last_upload(client) == {"upload_ms": 1791654963000, "device": "Watch"}
    assert garmin_recent.last_upload(FakeClient({})) == {"upload_ms": None, "device": None}
    with pytest.raises(ValueError, match="device response"):
        garmin_recent.last_upload(FakeClient(None))


def test_stale_days_are_missing_or_older_than_the_upload(tmp_path: Path) -> None:
    def path(day: date) -> Path:
        return tmp_path / f"{day}.json"

    upload_ms = 1_800_000_000_000
    for day, mtime in ((date(2026, 10, 9), upload_ms + 1000), (date(2026, 10, 8), upload_ms - 1)):
        path(day).write_text("{}")
        os.utime(path(day), (mtime / 1000, mtime / 1000))
    stale = garmin_recent.stale_days(TODAY, upload_ms, 3, path)
    assert stale == [date(2026, 10, 7), date(2026, 10, 8)]  # the 9th is newer than the upload
    assert garmin_recent.stale_days(TODAY, None, 3, path) == []  # no upload: nothing to redo


def test_fetch_summary_quotes_the_display_name() -> None:
    client = FakeClient({})
    data = garmin_recent.fetch_summary(client, date(2026, 10, 9))
    assert data["calendarDate"] == "2026-10-09"
    assert client.calls == [
        (
            "/usersummary-service/usersummary/daily/someone%2Fwith%3Fodd%20chars",
            {"calendarDate": "2026-10-09"},
        )
    ]
