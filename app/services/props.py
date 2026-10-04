"""Admin-created props and futures (D-041): the admin picks a template and its
parameters; the engine prices it (never by hand). Read the form, build the params from
live data (current streak, season-start weight...), preview, then create."""

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from pydantic import ValidationError
from sqlalchemy import Connection, Engine, select

from app.core.clock import Clock
from app.core.db import immediate
from app.domain.lines import Pricing
from app.domain.markets import TEMPLATES, MarketSpec, Timeframe
from app.domain.schedule import local_date
from app.models import Market, Season
from app.services import audit, instance
from app.services.instance import InstanceConfig
from app.services.ledger import active_season_id
from app.services.markets import insert_market, pricing_data
from app.services.observations import canonical_weigh_ins
from app.services.outbox import Category, enqueue

PROP_TEMPLATES = ("milestone_by", "streak_reaches", "beat_last_week", "future_total_change")
LABELS = {
    "milestone_by": "Milestone: a weigh-in reaches a weight by a date",
    "streak_reaches": "Streak: weigh-in streak or lower-each-day streak reaches N",
    "beat_last_week": "Week vs week: this week's total beats last week's",
    "future_total_change": "Future: weight change from season start to a date",
}


class PropError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True, slots=True)
class Preview:
    template: str
    spec: MarketSpec
    pricing: Pricing


def _streak(conn: Connection, today: date, kind: str) -> tuple[int, int | None]:
    """The streak ending today, and today's canonical weigh-in.
    weigh_in: consecutive days with a weigh-in. down: consecutive days each lower than
    the day before."""
    canon = {c.local_date: c.value for c in canonical_weigh_ins(conn, today - timedelta(60), today)}
    run, day = 0, today
    while day in canon:
        before = canon.get(day - timedelta(days=1))
        if kind == "down" and (before is None or canon[day] >= before):
            break
        run += 1
        day -= timedelta(days=1)
    return run, canon.get(today)


def _params(conn: Connection, today: date, template: str, form: dict[str, str]) -> dict[str, Any]:
    tomorrow = today + timedelta(days=1)
    try:
        if template == "milestone_by":
            season = active_season_id(conn)
            direction = (
                conn.execute(select(Season.direction).where(Season.id == season)).scalar_one()
                if season
                else None
            ) or "down"
            return {
                "start": tomorrow,
                "deadline": date.fromisoformat(form.get("deadline", "")),
                "threshold_x10": round(float(form.get("threshold", "")) * 10),
                "direction": direction,
            }
        if template == "streak_reaches":
            kind = form.get("kind", "weigh_in")
            current, last = _streak(conn, today, kind)
            return {
                "start": tomorrow,
                "deadline": date.fromisoformat(form.get("deadline", "")),
                "kind": kind,
                "n": int(form.get("n", "")),
                "current": current,
                "last_weight_x10": last,
            }
        if template == "beat_last_week":
            monday = tomorrow + timedelta(days=(7 - tomorrow.weekday()) % 7)
            return {"metric": form.get("metric", "steps"), "start": monday}
        if template == "future_total_change":
            season_id = active_season_id(conn)
            if season_id is None:
                raise PropError("No season is running.")
            started = conn.execute(
                select(Season.started_at).where(Season.id == season_id)
            ).scalar_one()
            first_day = local_date(started, instance.read(conn).tz)  # type: ignore[union-attr]
            first = canonical_weigh_ins(conn, first_day, today)
            if not first:
                raise PropError("There's no weigh-in since the season started yet.")
            return {
                "created": today,
                "day": date.fromisoformat(form.get("day", "")),
                "season_start": first[0].local_date,
                "start_weight_x10": first[0].value,
            }
    except ValueError as exc:
        raise PropError("Fill in every field with a valid value.") from exc
    raise PropError("Unknown template.")


def preview(
    conn: Connection, config: InstanceConfig, today: date, template: str, form: dict[str, str]
) -> Preview:
    if not config.flags.get("props_futures"):
        raise PropError("Props and futures are turned off (Settings).")
    if template not in PROP_TEMPLATES:
        raise PropError("Unknown template.")
    t = TEMPLATES[template]
    try:
        params = t.params_model.model_validate(_params(conn, today, template, form))
    except ValidationError as exc:
        message = exc.errors()[0].get("msg", "Invalid values.").removeprefix("Value error, ")
        raise PropError(message) from exc
    timeframe = Timeframe.FUTURE if template == "future_total_change" else Timeframe.PROP
    spec = t.spec(params, timeframe, config.schedule, config.tz, config.unit)
    pricing = t.price(spec, pricing_data(conn, today), config.unit, float(config.economy.hold))
    if pricing is None:
        raise PropError(
            "The engine won't price this: it needs 7+ recent weigh-ins (or history for the "
            "metric), and props that are close to certain aren't offered."
        )
    return Preview(template, spec, pricing)


def create(engine: Engine, clock: Clock, actor: Any, template: str, form: dict[str, str]) -> int:
    with immediate(engine) as conn:
        config = instance.read(conn)
        if config is None:
            raise PropError("Finish setup first.")
        today = local_date(clock.now(), config.tz)
        p = preview(conn, config, today, template, form)
        season_id = active_season_id(conn)
        if season_id is None:
            raise PropError("No season is running.")
        if conn.execute(select(Market.id).where(Market.dedupe_key == p.spec.dedupe_key)).first():
            raise PropError("That prop already exists.")
        if p.spec.lock_at <= clock.now():
            raise PropError("Too late today: props lock at the nightly bet lock. Try tomorrow.")
        market_id = insert_market(
            conn,
            season_id=season_id,
            spec=p.spec,
            pricing=p.pricing,
            now=clock.now(),
            origin="admin",
        )
        enqueue(
            conn,
            clock,
            category=Category.NEW_MARKETS,
            payload={
                "timeframe": p.spec.timeframe.value,
                "day": today.isoformat(),
                "markets": [
                    {
                        "market_id": market_id,
                        "title": p.spec.title,
                        "line_x10": p.pricing.line_x10,
                        "odds_over": p.pricing.odds_over,
                        "odds_under": p.pricing.odds_under,
                    }
                ],
            },
            dedupe_key=f"new_markets:admin:{market_id}",
        )
        audit.record(
            conn,
            clock,
            actor_id=actor.user_id,
            ts=actor.acted_at,
            action="market.create_prop",
            target=("market", market_id),
            after={"template": template, "params": p.spec.params, "title": p.spec.title},
            ip=actor.ip,
        )
    return market_id
