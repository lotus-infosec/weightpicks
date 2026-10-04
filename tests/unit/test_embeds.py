import json
from datetime import UTC, datetime
from typing import Any

import pytest

from app.notify import embeds
from app.services.secrets import WEBHOOK_CATEGORIES

NAMES = {1: "@everyone", 2: "<@123456> and @here", 3: "**bold** _x_"}


def ctx() -> embeds.Context:
    return embeds.Context(
        app_name="WeightPicks",
        base_url="https://picks.example.invalid",
        now=datetime(2026, 10, 5, 12, tzinfo=UTC),
        user_name=lambda uid: NAMES.get(uid or 0, "A player"),
        market_title=lambda mid: "Weight change, Mon Oct 5 → Tue Oct 6 (lb)",
    )


PAYLOADS: dict[str, dict[str, Any]] = {
    "bets_placed": {
        "user_id": 1,
        "market_id": 7,
        "side": "under",
        "american": -110,
        "line_x10": -5,
        "stake_cents": 2500,
    },
    "high_roller": {
        "user_id": 2,
        "market_id": 7,
        "side": "over",
        "american": 150,
        "line_x10": 79995,
        "stake_cents": 50000,
    },
    "bet_results": {
        "user_id": 1,
        "market_id": 7,
        "result": "won",
        "stake_cents": 2500,
        "payout_cents": 4773,
    },
    "market_settlements": {
        "market_id": 7,
        "title": "Steps on Tue",
        "winner": "over",
        "value_x10": 81230,
        "line_x10": 79995,
        "reason": "settled",
    },
    "busts": {"user_id": 3, "season_busts": 2},
    "new_markets": {
        "timeframe": "daily",
        "markets": [
            {"title": "Steps on Tue", "line_x10": 79995, "odds_over": -110, "odds_under": -110}
        ],
    },
    "weekly_standings": {
        "week": "2026-W41",
        "rows": [
            {"user_id": 1, "pnl_cents": 12000, "week_pnl_cents": -500, "busts": 0},
            {"user_id": 2, "pnl_cents": -3000, "busts": 1},
        ],
    },
    "admin_alerts": {"kind": "sync_failed", "error": "Garmin login failed: rerun garmin-login"},
}


@pytest.mark.parametrize("category", WEBHOOK_CATEGORIES)
def test_every_category_renders_without_pings(category: str) -> None:
    body = embeds.build(category, PAYLOADS.get(category, {"text": "hello"}), ctx())
    assert body["allowed_mentions"] == {"parse": []}
    assert body["username"] == "WeightPicks"
    (embed,) = body["embeds"]
    assert embed["title"] and embed["description"]
    assert isinstance(embed["color"], int)
    text = json.dumps(body)
    assert "@everyone" not in text and "@here" not in text and "<@123456>" not in text


def test_names_are_neutralised_and_escaped() -> None:
    assert embeds.safe("@everyone") == "@\u200beveryone"
    assert embeds.safe("<@123>") == "<\u200b@\u200b123\\>"
    assert embeds.safe("**bold**") == "\\*\\*bold\\*\\*"


@pytest.mark.parametrize(
    ("result", "colour", "word"),
    [
        ("won", embeds.GREEN, "WON"),
        ("lost", embeds.RED, "LOST"),
        ("push", embeds.GREY, "PUSH"),
        ("void", embeds.GREY, "VOID"),
    ],
)
def test_results_carry_colour_and_text(result: str, colour: int, word: str) -> None:
    body = embeds.build("bet_results", PAYLOADS["bet_results"] | {"result": result}, ctx())
    embed = body["embeds"][0]
    assert embed["color"] == colour and word in embed["title"]


def test_bet_won_text_and_links() -> None:
    embed = embeds.build("bet_results", PAYLOADS["bet_results"], ctx())["embeds"][0]
    assert "collected $47.73 on a $25.00 bet (+$22.73)" in embed["description"]
    assert embed["url"] == "https://picks.example.invalid/markets/7"
    placed = embeds.build("bets_placed", PAYLOADS["bets_placed"], ctx())["embeds"][0]
    assert "$25.00 on **UNDER -0.5** at -110" in placed["description"]


def test_settlement_push_and_void() -> None:
    push = embeds.build(
        "market_settlements",
        {"market_id": 7, "title": "T", "winner": None, "reason": "no_weigh_in"},
        ctx(),
    )["embeds"][0]
    assert push["title"] == "Market settled: PUSH" and push["color"] == embeds.GREY
    void = embeds.build(
        "market_settlements",
        {"market_id": 7, "title": "T", "result": "void", "reason": "bad line"},
        ctx(),
    )["embeds"][0]
    assert "refunded" in void["title"]


def test_test_posts_and_unknown_kinds() -> None:
    body = embeds.build("busts", {"kind": "test"}, ctx())
    assert body["embeds"][0]["title"] == "Webhook test"
    alert = embeds.build("admin_alerts", {"kind": "something_new"}, ctx())["embeds"][0]
    assert alert["title"] == "Admin alert: something new"


def test_money_and_numbers() -> None:
    assert embeds.money(-1250) == "-$12.50" and embeds.signed_money(500) == "+$5.00"
    assert embeds.number(79995) == "7,999.5" and embeds.number(80000) == "8,000"
    assert embeds.american(150) == "+150" and embeds.american(None) == "-"
