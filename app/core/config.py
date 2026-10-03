"""Install-time settings from the environment (BUILD_PLAN §1.7).

First-run and runtime settings live in the database and arrive in later stages.
`WP_IMAGE`, `WP_VERSION` and `WP_BIND` are read by Compose only.
"""

from functools import cached_property
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MIN_SECRET_KEY_LENGTH = 32


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", frozen=True)

    app_env: Literal["production", "dev"] = "production"
    app_secret_key: SecretStr = SecretStr("")
    wp_base_url: str = "http://127.0.0.1:8000"
    data_provider: Literal["garmindb", "simulated"] = "garmindb"
    sim_seed: int = 42
    log_level: Literal["DEBUG", "INFO", "WARNING"] = "INFO"
    log_format: Literal["json", "console"] = "json"
    ai_daily_neuron_cap: int = 5000
    backup_retention: int = 7
    data_dir: Path = Path("/data")
    # Overrides the SQLite file under data_dir (tests point this at a temp file).
    database_url: str | None = None
    # Instance time zone and weight unit; owned by /setup from STAGE08 (D-027).
    wp_timezone: str = "America/New_York"
    wp_unit: Literal["lb", "kg"] = "lb"
    # Session cookies are Secure (HTTPS-only). Dev may turn this off for a plain-http
    # LAN phone test; production refuses it (D-034).
    wp_cookie_secure: bool = True
    # Where the Docker build puts the compiled stylesheet (outside the dev source mount).
    wp_static_build_dir: Path = Path("/app/static-build")
    # GarminDB (D-038): its home (config, token, downloads, SQLite) and its own venv.
    garmin_home: Path = Path("/garmin")
    garmindb_python: Path = Path("/opt/garmindb/bin/python")

    @field_validator("wp_timezone")
    @classmethod
    def _known_time_zone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown time zone {value!r}") from exc
        return value

    @model_validator(mode="after")
    def _secret_key_required_in_production(self) -> "Settings":
        if (
            self.app_env == "production"
            and len(self.app_secret_key.get_secret_value()) < MIN_SECRET_KEY_LENGTH
        ):
            raise ValueError(
                f"APP_SECRET_KEY must be at least {MIN_SECRET_KEY_LENGTH} characters in production"
            )
        return self

    @model_validator(mode="after")
    def _secure_cookies_outside_dev(self) -> "Settings":
        if not self.wp_cookie_secure and self.app_env != "dev":
            raise ValueError("WP_COOKIE_SECURE=false is only allowed with APP_ENV=dev")
        return self

    @cached_property
    def db_url(self) -> str:
        return self.database_url or f"sqlite:///{self.data_dir / 'app.db'}"

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.wp_timezone)

    @property
    def is_dev(self) -> bool:
        return self.app_env == "dev"

    @property
    def sim_clock(self) -> bool:
        """Dev with simulated data runs on the persisted SimClock; real data always runs
        on real time, even in dev (STAGE10)."""
        return self.is_dev and self.data_provider == "simulated"
