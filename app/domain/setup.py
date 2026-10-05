"""First-run wizard steps: pure validation of submitted form values (BUILD_PLAN §1.5).

Each validator takes the raw form strings and returns (clean values, field errors).
Clean values are JSON-safe; they are stored in the setup draft and applied by the
finish transaction. Secrets and the admin password are handled by the service (they
are encrypted or hashed before they are stored).
"""

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import time
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.domain.economy import VIG_PRESETS, Economy
from app.domain.markets import COUNT_MARKET_METRICS, METRIC_LABELS, Schedule
from app.domain.money import parse_cents
from app.domain.units import parse_tenths

Errors = dict[str, str]
Result = tuple[dict[str, Any], Errors]
PALETTES = {"ember": "Ember", "lagoon": "Lagoon", "grove": "Grove", "slate": "Slate"}
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
WEBHOOK_RE = re.compile(r"https://(discord\.com|discordapp\.com)/api/webhooks/\d+/[\w-]+")
# A Cloudflare account ID is 32 hex digits; it becomes part of the API URL path.
ACCOUNT_ID_RE = re.compile(r"[0-9a-fA-F]{32}")
API_TOKEN_RE = re.compile(r"[\w-]{20,200}")
SMTP_TLS_MODES = ("starttls", "ssl", "none")
COMMON_ZONES = (
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Phoenix",
    "America/Los_Angeles",
    "America/Anchorage",
    "Pacific/Honolulu",
    "Europe/London",
    "Europe/Berlin",
    "Australia/Sydney",
)


@dataclass(frozen=True, slots=True)
class Step:
    number: int
    key: str
    title: str
    optional: bool = False


STEPS: tuple[Step, ...] = (
    Step(1, "admin", "Admin account"),
    Step(2, "subject", "Subject and goal"),
    Step(3, "schedule", "Time zone and schedule"),
    Step(4, "economy", "Economy"),
    Step(5, "stats", "Stats to bet on"),
    Step(6, "garmin", "Garmin", optional=True),
    Step(7, "ai", "Workers AI", optional=True),
    Step(8, "discord", "Discord webhooks", optional=True),
    Step(9, "smtp", "Email (SMTP)", optional=True),
    Step(10, "registration", "Registration code"),
    Step(11, "appearance", "Look and feel"),
    Step(12, "review", "Review and finish"),
)
REQUIRED = tuple(s.key for s in STEPS if not s.optional and s.key != "review")


def _text(form: Mapping[str, str], key: str) -> str:
    return " ".join(form.get(key, "").split())


def _wall(raw: str) -> time | None:
    try:
        return time.fromisoformat(raw.strip())
    except ValueError:
        return None


# ---- step validators ------------------------------------------------------------------


def subject(form: Mapping[str, str]) -> Result:
    errors: Errors = {}
    name = _text(form, "subject_name")
    if not 1 <= len(name) <= 40:
        errors["subject_name"] = "Enter a name (up to 40 characters)."
    unit = form.get("unit", "")
    if unit not in ("lb", "kg"):
        errors["unit"] = "Choose lb or kg."
    low, high = (60, 1500) if unit != "kg" else (30, 700)
    start, goal = (
        parse_tenths(form.get("start_weight", "")),
        parse_tenths(form.get("goal_weight", "")),
    )
    for key, value in (("start_weight", start), ("goal_weight", goal)):
        if value is None or not low * 10 <= value <= high * 10:
            errors[key] = f"Enter a weight between {low} and {high}."
    if not errors and start == goal:
        errors["goal_weight"] = "The goal must differ from the starting weight."
    if errors or start is None or goal is None:
        return {}, errors
    return {
        "subject_name": name,
        "unit": unit,
        "start_weight_x10": start,
        "goal_weight_x10": goal,
        "direction": "down" if goal < start else "up",
    }, {}


def schedule(form: Mapping[str, str]) -> Result:
    errors: Errors = {}
    zone = form.get("timezone", "").strip()
    try:
        ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError):
        errors["timezone"] = "Choose a time zone such as America/New_York."
    walls = {
        k: _wall(form.get(k, "")) for k in ("daily_drop", "bet_lock", "weekly_drop", "monthly_drop")
    }
    for key, value in walls.items():
        if value is None:
            errors[key] = "Enter a time like 18:00."
    try:
        weekday = int(form.get("weekly_drop_weekday", ""))
        if not 0 <= weekday <= 6:
            raise ValueError
    except ValueError:
        errors["weekly_drop_weekday"] = "Choose a day."
        weekday = 6
    daily, lock, weekly, monthly = (
        walls[k] for k in ("daily_drop", "bet_lock", "weekly_drop", "monthly_drop")
    )
    if errors or daily is None or lock is None or weekly is None or monthly is None:
        return {}, errors
    try:
        sched = Schedule(
            daily_drop=daily,
            bet_lock=lock,
            weekly_drop_weekday=weekday,
            weekly_drop=weekly,
            monthly_drop=monthly,
        )
    except ValueError as exc:
        return {}, {"schedule": str(exc).capitalize() + "."}
    return {
        "timezone": zone,
        "daily_drop": f"{sched.daily_drop:%H:%M}",
        "bet_lock": f"{sched.bet_lock:%H:%M}",
        "weekly_drop_weekday": sched.weekly_drop_weekday,
        "weekly_drop": f"{sched.weekly_drop:%H:%M}",
        "monthly_drop": f"{sched.monthly_drop:%H:%M}",
    }, {}


def economy(form: Mapping[str, str]) -> Result:
    errors: Errors = {}
    money = {}
    for key in ("starting_bankroll", "daily_allowance", "bailout", "high_roller"):
        cents = parse_cents(form.get(key, ""))
        if cents is None or cents < 0:
            errors[key] = "Enter a dollar amount like 1000 or 12.50."
        money[key] = cents or 0
    max_bet_raw = form.get("max_bet", "").strip()
    max_bet = None if max_bet_raw == "" else parse_cents(max_bet_raw)
    if max_bet_raw and max_bet is None:
        errors["max_bet"] = "Enter a dollar amount, or leave empty for no maximum."
    ints = {}
    for key in ("bailout_cooldown_days", "max_parlay_legs"):
        try:
            ints[key] = int(form.get(key, ""))
        except ValueError:
            errors[key] = "Enter a whole number."
            ints[key] = 0
    vig = form.get("vig", "")
    if vig not in VIG_PRESETS:
        errors["vig"] = "Choose a vig."
    # Optional (the /setup wizard doesn't ask): blank means the default, clamped (D-043).
    pool_raw = form.get("pool_buyin", "").strip()
    pool_buyin = parse_cents(pool_raw) if pool_raw else None
    if pool_raw and (pool_buyin is None or pool_buyin < 100):
        errors["pool_buyin"] = "Enter a dollar amount of at least 1."
    if errors:
        return {}, errors
    default = Economy()
    if pool_buyin is None:
        pool_buyin = min(default.pool_buyin_cents, max(100, money["starting_bankroll"] // 2))
    try:
        result = Economy(
            starting_bankroll_cents=money["starting_bankroll"],
            daily_allowance_cents=money["daily_allowance"],
            bailout_cents=money["bailout"],
            bailout_cooldown_days=ints["bailout_cooldown_days"],
            hold=VIG_PRESETS[vig],
            max_bet_cents=max_bet,
            max_parlay_legs=ints["max_parlay_legs"],
            high_roller_cents=money["high_roller"],
            pool_buyin_cents=pool_buyin,
        )
    except ValueError as exc:
        return {}, {"economy": str(exc).capitalize() + "."}
    return result.to_json(), {}


def stats(form: Mapping[str, str]) -> Result:
    chosen = [m for m in COUNT_MARKET_METRICS if form.get(f"metric_{m}") == "on"]
    return {"enabled_metrics": chosen}, {}


def acknowledge(form: Mapping[str, str]) -> Result:
    return {"seen": True}, {}


def appearance(form: Mapping[str, str]) -> Result:
    name = _text(form, "app_name")
    errors: Errors = {}
    if not 1 <= len(name) <= 40:
        errors["app_name"] = "Enter a name (up to 40 characters)."
    palette = form.get("palette", "")
    if palette not in PALETTES:
        errors["palette"] = "Choose a palette."
    return ({}, errors) if errors else ({"app_name": name, "palette": palette}, {})


def workers_ai_problem(account_id: str, token: str) -> dict[str, str]:
    """Errors keyed by secret name for the Workers AI account ID and token (blank means
    keep what is stored)."""
    errors: dict[str, str] = {}
    if account_id and not ACCOUNT_ID_RE.fullmatch(account_id):
        errors["workers_ai.account_id"] = "A Cloudflare account ID is 32 characters (0-9, a-f)."
    if token and not API_TOKEN_RE.fullmatch(token):
        errors["workers_ai.token"] = "That doesn't look like a Cloudflare API token."
    return errors


def webhook_problem(url: str) -> str | None:
    if url and not WEBHOOK_RE.fullmatch(url):
        return "Paste a Discord webhook URL (https://discord.com/api/webhooks/...)."
    return None


def smtp(form: Mapping[str, str]) -> Result:
    host = form.get("host", "").strip()
    if not host:
        return {"configured": False}, {}
    errors: Errors = {}
    try:
        port = int(form.get("port", ""))
        if not 1 <= port <= 65535:
            raise ValueError
    except ValueError:
        errors["port"] = "Enter a port such as 587."
        port = 0
    sender = form.get("from_address", "").strip()
    if "@" not in sender:
        errors["from_address"] = "Enter the From address."
    tls = form.get("tls", "starttls") or "starttls"
    if tls not in SMTP_TLS_MODES:
        errors["tls"] = "Choose STARTTLS, SSL/TLS or none."
    if errors:
        return {}, errors
    return {
        "configured": True,
        "host": host,
        "port": port,
        "tls": tls,
        "username": form.get("username", "").strip(),
        "from_address": sender,
    }, {}


VALIDATORS: dict[str, Callable[[Mapping[str, str]], Result]] = {
    "subject": subject,
    "schedule": schedule,
    "economy": economy,
    "stats": stats,
    "garmin": acknowledge,
    "appearance": appearance,
    "smtp": smtp,
}


def step(number: int) -> Step:
    for s in STEPS:
        if s.number == number:
            return s
    raise KeyError(number)


def missing_steps(draft: Mapping[str, Any]) -> list[Step]:
    return [s for s in STEPS if s.key in REQUIRED and s.key not in draft]


def metric_choices() -> list[tuple[str, str]]:
    return [(m, METRIC_LABELS[m]) for m in COUNT_MARKET_METRICS]


def money_text(cents: int | None) -> str:
    if cents is None:
        return ""
    whole, frac = divmod(cents, 100)
    return f"{whole}" if frac == 0 else f"{whole}.{frac:02d}"
