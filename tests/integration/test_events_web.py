"""Player side of special events (D-043): the Events tab, entering and changing a guess,
guesses hidden until the lock; the My bets season switcher; pool and goal embeds."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from app.core.clock import SystemClock
from app.core.db import immediate
from app.models import Account, InstanceSettingsRow, User
from app.notify import embeds
from app.services import auth, instance, pools
from app.web.main import create_app
from tests.integration import web
from tests.integration.world import World, local


@pytest.fixture
def player(world: World) -> Iterator[tuple[TestClient, World, int]]:
    with immediate(world.engine) as conn:
        flags = conn.execute(select(InstanceSettingsRow.flags)).scalar_one() or {}
        conn.execute(
            update(InstanceSettingsRow).values(flags=dict(flags) | {"special_events": True})
        )
    with world.engine.connect() as conn:
        config = instance.read(conn)
    assert config is not None
    raw = {
        "title": "Columbus Day pot",
        "question": "What will the scale say on Monday Oct 12?",
        "target_date": "2026-10-12",
        "buy_in_cents": 2_500,
    }
    pool_id = pools.create(
        world.engine, world.clock, pools.validate(config, world.clock.now(), raw), actor=None
    )
    c = web.client(create_app(world.settings, domain_clock=world.clock))
    c.__enter__()
    web.register(c, auth.rotate_registration_code(world.engine, SystemClock()))
    yield c, world, pool_id
    c.__exit__(None, None, None)


def enter(c: TestClient, pool_id: int, guess: str) -> str:
    token = web.page_csrf(c, "/?tab=events")
    r = c.post(
        f"/api/pools/{pool_id}/enter",
        data={"csrf_token": token, "guess": guess},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    return str(r.text)


def test_events_tab_enter_change_and_hidden_guesses(player: tuple[TestClient, World, int]) -> None:
    c, w, pool_id = player
    page = c.get("/?tab=events").text
    assert ">Events</a>" in page and "Columbus Day pot" in page
    assert "Buy in for $25.00" in page
    joined = enter(c, pool_id, "219.5")
    assert "Good luck!" in joined and "Change guess" in joined
    assert 'value="219.5"' in joined and "$25.00</strong>" in joined  # the pot
    changed = enter(c, pool_id, "218.8")
    assert "Guess updated." in changed and 'value="218.8"' in changed
    bad = enter(c, pool_id, "lots")
    assert "Enter your guess as a weight" in bad
    with w.engine.connect() as conn:
        balance = conn.execute(
            select(Account.balance_cents)
            .join(User, User.id == Account.user_id)
            .where(User.role == "player")
        ).scalar_one()
    assert balance == 100_000 - 2_500  # one buy-in
    # Another player's guess is hidden while open, shown once locked.
    rival = _rival(w)
    pools.enter(w.engine, w.clock, user_id=rival, pool_id=pool_id, guess_x10=2170)
    assert "Rival" not in c.get("/?tab=events").text
    w.clock.set(local(2026, 10, 11, 22, 5))
    locked = c.get("/?tab=events").text
    assert "Rival: 217.0" in locked and "Your guess: <strong>218.8" in locked
    late = enter(c, pool_id, "219.0")
    assert "This pool is locked." in late


def _rival(w: World) -> int:
    from app.services import ledger
    from app.services.users import ensure_player

    with immediate(w.engine) as conn:
        season = ledger.active_season_id(conn)
        assert season is not None
        user = ensure_player(conn, w.clock, "rival@example.invalid", "Rival")
        account = ledger.open_player_account(conn, w.clock, season, user)
        ledger.grant_starting(conn, w.clock, account, 100_000, idempotency_key=f"grant:{user}")
    return user


def test_full_page_pool_entry_keeps_the_parlay_leg_limit(
    player: tuple[TestClient, World, int],
) -> None:
    """Regression: the full-page answer to entering a pool hardcoded 6 legs."""
    c, w, pool_id = player
    with immediate(w.engine) as conn:
        economy = conn.execute(select(InstanceSettingsRow.economy)).scalar_one() or {}
        conn.execute(
            update(InstanceSettingsRow).values(economy=dict(economy) | {"max_parlay_legs": 3})
        )
    r = c.post(
        f"/api/pools/{pool_id}/enter",
        data={"csrf_token": web.page_csrf(c, "/?tab=events"), "guess": "219.5"},
    )
    assert r.status_code == 200 and 'data-max-legs="3"' in r.text


def test_events_tab_hidden_when_flag_off(player: tuple[TestClient, World, int]) -> None:
    c, w, _ = player
    with immediate(w.engine) as conn:
        conn.execute(update(InstanceSettingsRow).values(flags={"registration_open": True}))
    page = c.get("/?tab=events").text
    assert ">Events</a>" not in page and "Columbus Day pot" not in page


def test_my_bets_season_switcher(player: tuple[TestClient, World, int]) -> None:
    c, w, _ = player
    assert 'data-testid="season-switcher"' not in c.get("/bets/mine").text  # one season
    from app.services import ledger

    with immediate(w.engine) as conn:
        from app.models import Season

        conn.execute(update(Season).values(ended_at=w.clock.now(), status="ended"))
        ledger.open_season(conn, w.clock)
    page = c.get("/bets/mine").text
    assert 'data-testid="season-switcher"' in page and "Season 2 (now)" in page
    assert c.get("/bets/mine?season=1").status_code == 200
    assert c.get("/bets/mine?season=999").status_code == 200  # unknown -> current


def ctx() -> embeds.Context:
    names = {1: "Sam", 2: "@everyone"}
    return embeds.Context(
        "WP", "https://wp.example", local(2026, 10, 12, 12), lambda u: names.get(u or 0, "?"), str
    )


def test_pool_and_goal_embeds() -> None:
    opened = embeds.build(
        "special_events",
        {
            "kind": "pool_open",
            "title": "Columbus pot",
            "question": "Weight on Oct 12?",
            "target_date": "2026-10-12",
            "buy_in_cents": 2_500,
        },
        ctx(),
    )["embeds"][0]
    assert opened["title"] == "New pot: Columbus pot" and "$25.00" in opened["description"]
    won = embeds.build(
        "special_events",
        {
            "kind": "pool_result",
            "title": "Columbus pot",
            "status": "settled",
            "result_x10": 2194,
            "winners": [1, 2],
            "pot_cents": 5_000,
            "share_cents": 2_500,
        },
        ctx(),
    )
    text = won["embeds"][0]["description"]
    assert "219.4" in text and "Sam" in text and "@everyone" not in text
    assert won["allowed_mentions"] == {"parse": []}
    refunded = embeds.build(
        "special_events",
        {"kind": "pool_result", "title": "X", "status": "refunded", "reason": "nobody_eligible"},
        ctx(),
    )["embeds"][0]
    assert "every guess was over" in refunded["description"]
    goal = embeds.build(
        "goal_reached",
        {
            "kind": "goal_reached",
            "season": 1,
            "start_x10": 2216,
            "goal_x10": 2200,
            "value_x10": 2194,
            "day": "2026-10-11",
            "days": 6,
            "top": [{"user_id": 1, "pnl_cents": 1_234}],
        },
        ctx(),
    )["embeds"][0]
    assert goal["title"] == "GOAL REACHED"
    assert "221.6 → 220" in goal["description"] and "1st: Sam (+$12.34)" in goal["description"]
    start = embeds.build(
        "goal_reached",
        {"kind": "season_start", "season": 2, "start_x10": 2194, "goal_x10": 2100},
        ctx(),
    )["embeds"][0]
    assert start["title"] == "A new season begins" and "210" in start["description"]
