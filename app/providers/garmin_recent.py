"""Fetch the last few days of weigh-ins with the saved Garmin token (D-038).

GarminDB's download range stops at yesterday, so a weigh-in made this morning would
not arrive until tomorrow. This runs right after GarminDB on every sync, under
GarminDB's venv (standard library + garminconnect only), and writes one file that the
provider reads next to GarminDB's own `weight_YYYY-MM-DD.json` files. GarminDB's
importer ignores it (its pattern needs a date).
"""

import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

HOME = Path(os.environ.get("HOME", "/garmin"))
TOKEN_FILE = HOME / ".GarminDb" / "garmin_tokens.json"
OUT_FILE = HOME / "HealthData" / "Weight" / "weight_recent.json"
DAYS = 3
WEIGHT_URL = "/weight-service/weight/dateRange"


def fetch(client: Any, today: date) -> dict[str, Any]:
    start = today - timedelta(days=DAYS)
    data: dict[str, Any] = client.connectapi(
        WEIGHT_URL, params={"startDate": start.isoformat(), "endDate": today.isoformat()}
    )
    if not isinstance(data, dict) or not isinstance(data.get("dateWeightList"), list):
        raise ValueError("unexpected weight response shape")
    return data


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
        data = fetch(client, date.fromisoformat(sys.argv[1]))
        write(data, OUT_FILE)
    except Exception as exc:  # the runner records only the class name
        print(f"recent weigh-ins failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    print(f"recent weigh-ins: {len(data['dateWeightList'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
