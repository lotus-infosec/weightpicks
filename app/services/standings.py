"""Weekly standings post: Monday 09:00, the top 10 by settled season P&L with last week's
change (bets and events that finished Mon-Sun, local) and wins."""

from datetime import date, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import Engine

from app.core.clock import Clock
from app.core.db import immediate
from app.domain.schedule import at_local
from app.services import leaderboard
from app.services.ledger import active_season_id
from app.services.outbox import Category, enqueue

TOP = 10


def post_week(engine: Engine, clock: Clock, tz: ZoneInfo, monday: date) -> bool:
    """Queue the standings for the week that ended the day before `monday`."""
    start = at_local(monday - timedelta(days=7), time(0), tz)
    end = at_local(monday, time(0), tz)
    with immediate(engine) as conn:
        season_id = active_season_id(conn)
        rows = leaderboard.standings(conn, season_id)[:TOP]
        if not rows or season_id is None:
            return False
        week = leaderboard.settled_pnl(conn, season_id, since=start, until=end)
        year, iso_week, _ = (monday - timedelta(days=7)).isocalendar()
        return enqueue(
            conn,
            clock,
            category=Category.WEEKLY_STANDINGS,
            payload={
                "week": f"{year}-W{iso_week:02d}",
                "rows": [
                    {
                        "user_id": r.user_id,
                        "pnl_cents": r.pnl_cents,
                        "week_pnl_cents": week.get(r.user_id, 0),
                        "wins": r.wins,
                    }
                    for r in rows
                ],
            },
            dedupe_key=f"weekly_standings:{year}-W{iso_week:02d}",
        )
