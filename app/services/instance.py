"""The singleton instance settings row: the source of truth for zone, unit, schedule,
enabled metrics, economy, appearance, flags and instance state.

`/setup` writes it. Tests create it from the environment defaults (`ensure`).
"""

from dataclasses import asdict, dataclass, field
from datetime import time
from typing import Any, get_args
from zoneinfo import ZoneInfo

from sqlalchemy import Connection, Engine, insert, select, update

from app.core.clock import Clock
from app.core.config import Settings
from app.core.db import immediate
from app.domain.economy import Economy
from app.domain.markets import COUNT_MARKET_METRICS, Schedule
from app.domain.units import Unit
from app.models import InstanceSettingsRow

ACTIVE, FROZEN = "active", "frozen"


FLAG_DEFAULTS: dict[str, bool] = {  # optional features start off
    "real_garmin": False,
    "registration_open": False,
    "discord_public": False,
    "props_futures": False,
    "parlays": False,
    "ai_props": False,
    "ai_hype": False,
    "special_events": False,
    "backup_ui": False,
}
PALETTES = ("ember", "lagoon", "grove", "slate")


@dataclass(frozen=True, slots=True)
class InstanceConfig:
    timezone: str
    unit: Unit
    schedule: Schedule
    enabled_metrics: tuple[str, ...]
    state: str
    app_name: str = "WeightPicks"
    palette: str = "ember"
    subject_name: str | None = None
    economy: Economy = field(default_factory=Economy)
    ai_mode: str = "review"
    flags: dict[str, bool] = field(default_factory=lambda: dict(FLAG_DEFAULTS))
    setup_completed: bool = False
    public_url: str | None = None  # Admin → Settings; overrides WP_BASE_URL

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


def schedule_to_json(schedule: Schedule) -> dict[str, Any]:
    return {
        k: v.strftime("%H:%M") if isinstance(v, time) else v for k, v in asdict(schedule).items()
    }


def schedule_from_json(data: dict[str, Any]) -> Schedule:
    """Missing keys fall back to the defaults; Schedule validates the result."""
    d = Schedule()

    def wall(key: str, default: time) -> time:
        value = data.get(key)
        return default if value is None else time.fromisoformat(value)

    return Schedule(
        daily_drop=wall("daily_drop", d.daily_drop),
        bet_lock=wall("bet_lock", d.bet_lock),
        weekly_drop_weekday=int(data.get("weekly_drop_weekday", d.weekly_drop_weekday)),
        weekly_drop=wall("weekly_drop", d.weekly_drop),
        monthly_drop=wall("monthly_drop", d.monthly_drop),
        weigh_in_end=wall("weigh_in_end", d.weigh_in_end),
    )


def read(conn: Connection) -> InstanceConfig | None:
    t = InstanceSettingsRow
    row = conn.execute(
        select(
            t.timezone,
            t.unit,
            t.schedule,
            t.enabled_metrics,
            t.instance_state,
            t.app_name,
            t.palette,
            t.subject_name,
            t.economy,
            t.ai_mode,
            t.flags,
            t.setup_completed_at,
            t.public_url,
        ).where(t.id == 1)
    ).one_or_none()
    if row is None:
        return None
    if row.unit not in get_args(Unit):
        raise ValueError(f"unknown unit {row.unit!r}")
    return InstanceConfig(
        timezone=row.timezone,
        unit=row.unit,
        schedule=schedule_from_json(row.schedule),
        enabled_metrics=tuple(m for m in row.enabled_metrics if m in COUNT_MARKET_METRICS),
        state=row.instance_state,
        app_name=row.app_name,
        palette=row.palette if row.palette in PALETTES else "ember",
        subject_name=row.subject_name,
        economy=Economy.from_json(row.economy),
        ai_mode=row.ai_mode,
        flags=FLAG_DEFAULTS
        | {k: bool(v) for k, v in (row.flags or {}).items() if k in FLAG_DEFAULTS},
        setup_completed=row.setup_completed_at is not None,
        public_url=row.public_url,
    )


def economy(conn: Connection) -> Economy:
    """The live economy settings (defaults if the row doesn't exist yet)."""
    config = read(conn)
    return config.economy if config else Economy()


def ensure(conn: Connection, clock: Clock, settings: Settings) -> InstanceConfig:
    """Read the row, creating it from the environment defaults on first use."""
    existing = read(conn)
    if existing is not None:
        return existing
    conn.execute(
        insert(InstanceSettingsRow).values(
            id=1,
            timezone=settings.wp_timezone,
            unit=settings.wp_unit,
            schedule=schedule_to_json(Schedule()),
            enabled_metrics=list(COUNT_MARKET_METRICS),
            instance_state=ACTIVE,
            updated_at=clock.now(),
            economy=Economy().to_json(),
            flags=dict(FLAG_DEFAULTS),
        )
    )
    created = read(conn)
    if created is None:  # pragma: no cover - the insert above just succeeded
        raise LookupError("settings row missing after insert")
    return created


def load(engine: Engine, clock: Clock, settings: Settings) -> InstanceConfig:
    with engine.connect() as conn:  # no write lock once the row exists
        existing = read(conn)
    if existing is not None:
        return existing
    with immediate(engine) as conn:
        return ensure(conn, clock, settings)


def current_state(conn: Connection) -> str:
    state = conn.execute(
        select(InstanceSettingsRow.instance_state).where(InstanceSettingsRow.id == 1)
    ).scalar_one_or_none()
    return state or ACTIVE


def set_instance_state(conn: Connection, clock: Clock, state: str) -> int:
    """Freeze or unfreeze the instance. Freezing locks every open market in the same
    transaction, so no bet can slip in. Returns the number of markets locked."""
    from app.services.markets import lock_open_markets

    if state not in (ACTIVE, FROZEN):
        raise ValueError(f"unknown instance state {state!r}")
    now = clock.now()
    result = conn.execute(
        update(InstanceSettingsRow)
        .where(InstanceSettingsRow.id == 1)
        .values(instance_state=state, updated_at=now)
    )
    if result.rowcount != 1:
        raise LookupError("the settings row does not exist yet")
    return lock_open_markets(conn, now, everything=True) if state == FROZEN else 0


def public_url(config: InstanceConfig | None, settings: Settings) -> tuple[str, str]:
    """The base of every link sent out, and where it came from: the admin's setting
    ("admin") or WP_BASE_URL (".env"). Never the request's Host header."""
    if config is not None and config.public_url:
        return config.public_url, "admin"
    return settings.wp_base_url.rstrip("/"), ".env"
