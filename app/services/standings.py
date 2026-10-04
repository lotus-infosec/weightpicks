"""Weekly standings post (BUILD_PLAN §1.4.6): Monday 09:00, the top 10 by season P&L with
last week's P&L change (week = Mon-Sun, local)."""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import Connection, Engine, func, select

from app.core.clock import Clock
from app.core.db import immediate
from app.domain.ledger import BETTING_KINDS
from app.domain.schedule import at_local
from app.models import Account, LedgerEntry, LedgerTxn
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
        if not rows:
            return False
        week = _week_pnl(conn, [r.user_id for r in rows], season_id, start, end)
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
                        "balance_cents": r.balance_cents,
                        "week_pnl_cents": week.get(r.user_id, 0),
                        "busts": r.busts,
                    }
                    for r in rows
                ],
            },
            dedupe_key=f"weekly_standings:{year}-W{iso_week:02d}",
        )


def _week_pnl(
    conn: Connection, user_ids: list[int], season_id: int | None, start: datetime, end: datetime
) -> dict[int, int]:
    result = conn.execute(
        select(Account.user_id, func.sum(LedgerEntry.amount_cents))
        .join(LedgerEntry, LedgerEntry.account_id == Account.id)
        .join(LedgerTxn, LedgerTxn.id == LedgerEntry.txn_id)
        .where(
            Account.season_id == season_id,
            Account.user_id.in_(user_ids),
            LedgerEntry.kind.in_([k.value for k in BETTING_KINDS]),
            LedgerTxn.created_at >= start,
            LedgerTxn.created_at < end,
        )
        .group_by(Account.user_id)
    )
    return {uid: int(total) for uid, total in result if uid is not None}
