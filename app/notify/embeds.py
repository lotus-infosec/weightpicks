"""Discord webhook bodies for outbox rows (BUILD_PLAN §2.5). Pure: no I/O.

Every body carries `allowed_mentions: {"parse": []}`, so a player named `@everyone` can
never ping a server; names are also escaped and their `@` neutralised in case a body is
ever posted without it. Colour encodes the result, and the text always says it too.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

GREEN, RED, GREY, BLUE, GOLD, AMBER, BLURPLE = (
    0x2ECC71,
    0xE74C3C,
    0x95A5A6,
    0x3498DB,
    0xF1C40F,
    0xE67E22,
    0x5865F2,
)
RESULT_COLOURS = {"won": GREEN, "lost": RED, "push": GREY, "void": GREY}
RESULT_WORDS = {"won": "won", "lost": "lost", "push": "pushed", "void": "was refunded"}
CATEGORY_LABELS = {
    "bets_placed": "Bets placed",
    "high_roller": "High rollers",
    "bet_results": "Bet results",
    "parlay_results": "Parlay results",
    "market_settlements": "Market settlements",
    "new_markets": "New markets",
    "special_events": "Special events",
    "hype": "Hype",
    "weekly_standings": "Weekly standings",
    "busts": "Busts",
    "goal_reached": "Goal reached",
    "admin_alerts": "Admin alerts",
}
_MARKDOWN = re.compile(r"([\\*_~`|>#\[\]()-])")
ZWSP = "​"
DESCRIPTION_LIMIT = 4000
FIELD_LIMIT = 25


@dataclass(frozen=True, slots=True)
class Context:
    app_name: str
    base_url: str
    now: datetime
    user_name: Callable[[int | None], str]
    market_title: Callable[[int | None], str]


def safe(text: object, limit: int = 256) -> str:
    """Escape Discord markdown and break every mention (`@everyone`, `<@123>`, `<#1>`)."""
    value = _MARKDOWN.sub(r"\\\1", str(text))
    value = value.replace("@", "@" + ZWSP).replace("<", "<" + ZWSP)
    return value[:limit]


def money(cents: int | None) -> str:
    cents = cents or 0
    sign = "-" if cents < 0 else ""
    return f"{sign}${abs(cents) / 100:,.2f}"


def signed_money(cents: int | None) -> str:
    return ("+" if (cents or 0) > 0 else "") + money(cents)


def american(odds: int | None) -> str:
    if odds is None:
        return "-"
    return f"+{odds}" if odds > 0 else str(odds)


def number(x10: int | None) -> str:
    if x10 is None:
        return "-"
    return f"{x10 / 10:,.1f}" if x10 % 10 else f"{x10 // 10:,}"


def _market_link(ctx: Context, market_id: int | None) -> str | None:
    return f"{ctx.base_url.rstrip('/')}/markets/{market_id}" if market_id else None


def _embed(
    ctx: Context,
    category: str,
    title: str,
    description: str,
    colour: int,
    *,
    fields: list[dict[str, Any]] | None = None,
    url: str | None = None,
) -> dict[str, Any]:
    embed: dict[str, Any] = {
        "title": title[:256],
        "description": description[:DESCRIPTION_LIMIT],
        "color": colour,
        "timestamp": ctx.now.isoformat(),
        "footer": {"text": f"{ctx.app_name} · {CATEGORY_LABELS.get(category, category)}"[:2048]},
    }
    if fields:
        embed["fields"] = fields[:FIELD_LIMIT]
    if url:
        embed["url"] = url
    return {
        "username": ctx.app_name[:80],
        "embeds": [embed],
        "allowed_mentions": {"parse": []},
    }


def _bet_placed(category: str, p: dict[str, Any], ctx: Context) -> dict[str, Any]:
    who = safe(ctx.user_name(p.get("user_id")))
    if p.get("kind") == "parlay":
        title = "High roller parlay!" if category == "high_roller" else "New parlay"
        text = (
            f"**{who}** bet {money(p.get('stake_cents'))} on a **{p.get('legs')}-leg parlay** "
            f"at {american(p.get('american'))}"
        )
        return _embed(ctx, category, title, text, GOLD if category == "high_roller" else BLURPLE)
    market = safe(ctx.market_title(p.get("market_id")))
    side = str(p.get("side", "")).upper()
    line = f" {number(p['line_x10'])}" if p.get("line_x10") is not None else ""
    title = "High roller!" if category == "high_roller" else "New bet"
    text = (
        f"**{who}** bet {money(p.get('stake_cents'))} on **{side}{line}** "
        f"at {american(p.get('american'))}\n{market}"
    )
    colour = GOLD if category == "high_roller" else BLURPLE
    return _embed(ctx, category, title, text, colour, url=_market_link(ctx, p.get("market_id")))


def _bet_result(category: str, p: dict[str, Any], ctx: Context) -> dict[str, Any]:
    result = str(p.get("result", ""))
    who = safe(ctx.user_name(p.get("user_id")))
    market = safe(ctx.market_title(p.get("market_id"))) if p.get("market_id") else ""
    stake, paid = p.get("stake_cents") or 0, p.get("payout_cents") or 0
    if result == "won":
        detail = f"collected {money(paid)} on a {money(stake)} bet (+{money(paid - stake)})"
    elif result == "lost":
        detail = f"lost a {money(stake)} bet"
    else:
        detail = f"got the {money(stake)} stake back"
    reason = f" ({safe(p['reason'])})" if p.get("reason") else ""
    if p.get("kind") == "parlay":
        market = f"{p.get('legs')}-leg parlay"
    title = f"Bet {RESULT_WORDS.get(result, result)}: {result.upper()}"
    text = f"**{who}** {detail}{reason}" + (f"\n{market}" if market else "")
    colour = RESULT_COLOURS.get(result, GREY)
    return _embed(ctx, category, title, text, colour, url=_market_link(ctx, p.get("market_id")))


def _market_settled(category: str, p: dict[str, Any], ctx: Context) -> dict[str, Any]:
    title_text = safe(p.get("title") or ctx.market_title(p.get("market_id")))
    url = _market_link(ctx, p.get("market_id"))
    if p.get("result") == "void":
        reason = safe(p.get("reason") or "voided by the admin")
        return _embed(
            ctx,
            category,
            "Market voided: all stakes refunded",
            f"{title_text}\n{reason}",
            GREY,
            url=url,
        )
    winner = p.get("winner")
    if winner is None:
        reason = safe(p.get("reason") or "no result")
        text = f"{title_text}\nResult: **PUSH** ({reason}), stakes refunded"
        return _embed(ctx, category, "Market settled: PUSH", text, GREY, url=url)
    text = (
        f"{title_text}\nResult: **{str(winner).upper()}** "
        f"(actual {number(p.get('value_x10'))} vs line {number(p.get('line_x10'))})"
    )
    return _embed(ctx, category, f"Market settled: {str(winner).upper()}", text, BLUE, url=url)


def _bust(category: str, p: dict[str, Any], ctx: Context) -> dict[str, Any]:
    who = safe(ctx.user_name(p.get("user_id")))
    n = p.get("season_busts") or 1
    text = (
        f"**{who}** went bust (bust #{n} this season). Bailouts are available after the cooldown."
    )
    return _embed(ctx, category, "Bust!", text, RED)


def _new_markets(category: str, p: dict[str, Any], ctx: Context) -> dict[str, Any]:
    fields = [
        {
            "name": safe(m.get("title", ""), 256),
            "value": (
                f"Line {number(m.get('line_x10'))} · Over {american(m.get('odds_over'))}"
                f" · Under {american(m.get('odds_under'))}"
            ),
            "inline": False,
        }
        for m in p.get("markets", [])
    ]
    timeframe = str(p.get("timeframe", "")).capitalize()
    title = f"{timeframe} markets are open"
    text = f"{len(fields)} new market(s). Bets lock at the time shown on each market."
    return _embed(ctx, category, title, text, BLUE, fields=fields, url=ctx.base_url)


def _standings(category: str, p: dict[str, Any], ctx: Context) -> dict[str, Any]:
    lines = []
    for i, row in enumerate(p.get("rows", []), 1):
        badge = f" · busts {row['busts']}" if row.get("busts") else ""
        week = row.get("week_pnl_cents")
        week_text = f" (week {signed_money(week)})" if week is not None else ""
        lines.append(
            f"{i}. **{safe(ctx.user_name(row.get('user_id')))}** "
            f"{signed_money(row.get('pnl_cents'))}{week_text}{badge}"
        )
    text = "\n".join(lines) or "No players yet."
    title = f"Weekly standings · {safe(p.get('week', ''))}"
    return _embed(ctx, category, title, text, GOLD, url=f"{ctx.base_url.rstrip('/')}/leaderboard")


ALERT_TEXT = {
    "sync_failed": lambda p, ctx: f"Garmin sync failed: {safe(p.get('error'), 1000)}",
    "stale_market": lambda p, ctx: (
        f"{safe(ctx.market_title(p.get('market_id')))} is waiting to settle "
        f"({safe(p.get('waiting_for'))}). Nothing settles on stale data."
    ),
    "delivery_failed": lambda p, ctx: (
        f"A {CATEGORY_LABELS.get(str(p.get('category')), p.get('category'))} post could not be "
        f"delivered: {safe(p.get('error'), 500)}"
    ),
}


def _alert(category: str, p: dict[str, Any], ctx: Context) -> dict[str, Any]:
    kind = str(p.get("kind", "alert"))
    render = ALERT_TEXT.get(kind)
    text = render(p, ctx) if render else safe(p.get("text") or kind, 1000)
    return _embed(ctx, category, f"Admin alert: {kind.replace('_', ' ')}", text, AMBER)


def _test(category: str, p: dict[str, Any], ctx: Context) -> dict[str, Any]:
    label = CATEGORY_LABELS.get(category, category)
    text = f"Test post for **{label}**. If you can read this, the webhook works."
    return _embed(ctx, category, "Webhook test", text, GREEN)


def _fallback(category: str, p: dict[str, Any], ctx: Context) -> dict[str, Any]:
    text = safe(p.get("text") or p.get("title") or CATEGORY_LABELS.get(category, category), 1000)
    return _embed(ctx, category, CATEGORY_LABELS.get(category, category), text, BLURPLE)


BUILDERS: dict[str, Callable[[str, dict[str, Any], Context], dict[str, Any]]] = {
    "bets_placed": _bet_placed,
    "high_roller": _bet_placed,
    "bet_results": _bet_result,
    "market_settlements": _market_settled,
    "busts": _bust,
    "new_markets": _new_markets,
    "weekly_standings": _standings,
    "admin_alerts": _alert,
}


def build(category: str, payload: dict[str, Any], ctx: Context) -> dict[str, Any]:
    if payload.get("kind") == "test":
        return _test(category, payload, ctx)
    return BUILDERS.get(category, _fallback)(category, payload, ctx)
