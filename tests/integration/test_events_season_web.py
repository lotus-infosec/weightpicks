"""Admin → Events (AI draft and manual, preview, publish, void) and Admin → Season
(freeze, Goal Reached, new season) over HTTP (D-043)."""

from collections.abc import Iterator
from datetime import date
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select, update

from app.core.clock import SimClock
from app.core.db import immediate
from app.models import AiRun, AuditEntry, InstanceSettingsRow, Pool, Season
from app.services import events, instance, secrets
from app.web.main import create_app
from tests.fake_ai import canned
from tests.integration import web
from tests.integration.world import World, local

KEY = "k" * 40


def flags(w: World, **values: bool) -> None:
    with immediate(w.engine) as conn:
        current = conn.execute(select(InstanceSettingsRow.flags)).scalar_one() or {}
        conn.execute(update(InstanceSettingsRow).values(flags=dict(current) | values))


@pytest.fixture
def admin(world: World) -> Iterator[tuple[TestClient, World]]:
    world.settings = world.settings.model_copy(update={"app_secret_key": SecretStr(KEY)})
    flags(world, special_events=True)
    c = web.client(create_app(world.settings, domain_clock=world.clock))
    c.__enter__()
    web.as_admin(c, world.engine)
    yield c, world
    c.__exit__(None, None, None)


def post(c: TestClient, page: str, action: str, data: dict[str, str] | None = None) -> Any:
    token = web.page_csrf(c, page)
    return c.post(action, data={"csrf_token": token, **(data or {})}, follow_redirects=False)


EVENT = {
    "title": "Columbus Day pot",
    "question": "What will the scale say on Monday Oct 12?",
    "target_date": "2026-10-12",
    "buy_in": "25",
}


def store_token(w: World) -> None:
    with immediate(w.engine) as conn:
        secrets.put(conn, w.clock, KEY, "workers_ai.account_id", "acct")
        secrets.put(conn, w.clock, KEY, "workers_ai.token", "tok")


def ai_draft(w: World, reply: Any, status: int = 200) -> events.AiDraft:
    return events.draft_with_ai(
        w.engine,
        w.clock,
        SimClock(local(2026, 10, 5, 12)),
        w.settings,
        "Columbus Day pot, $25, guess my weight that morning",
        transport=canned(reply, status=status).transport,
    )


def test_ai_draft_valid_clamped_refused_and_failed(admin: tuple[TestClient, World]) -> None:
    _c, w = admin
    store_token(w)
    good = {
        "title": "Columbus Day pot",
        "question": "Guess the weigh-in on Mon Oct 12.",
        "target_date": "2026-10-12",
        "buy_in_dollars": 25,
    }
    result = ai_draft(w, good)
    assert result.draft is not None and result.error is None
    assert (result.draft.target_date, result.draft.buy_in_cents) == (date(2026, 10, 12), 2_500)
    big = ai_draft(w, good | {"buy_in_dollars": 5000})
    assert big.draft is not None and big.draft.buy_in_cents == 50_000 and big.draft.notes
    for bad, reason in (
        (good | {"target_date": "2027-06-01"}, "target_date"),
        (good | {"title": "Click https://evil.example"}, "text"),
        (good | {"question": ""}, "question"),
    ):
        refused = ai_draft(w, bad)
        assert refused.draft is None and refused.error and "refused" in refused.error
        assert refused.raw is not None
        with w.engine.connect() as conn:
            errors = conn.execute(
                select(AiRun.errors).where(AiRun.id == refused.run_id)
            ).scalar_one()
        assert errors == [{"reason": reason}]
    broken = ai_draft(w, "{nope")
    assert broken.draft is None and "by hand" in (broken.error or "")
    with w.engine.connect() as conn:
        kinds = set(conn.execute(select(AiRun.kind)).scalars())
    assert kinds == {"event_builder"}
    assert events.draft_with_ai(w.engine, w.clock, w.clock, w.settings, "  ").error


def test_ai_draft_without_token_says_fill_by_hand(admin: tuple[TestClient, World]) -> None:
    _c, w = admin
    result = ai_draft(w, {})
    assert result.draft is None and "isn't set up" in (result.error or "")


def test_manual_preview_publish_and_void(admin: tuple[TestClient, World]) -> None:
    c, w = admin
    page = c.get("/admin/events").text
    assert 'data-testid="event-form"' in page
    assert 'data-testid="event-ai-form"' not in page  # no token: no AI box
    preview = post(c, "/admin/events", "/admin/events/preview", EVENT)
    assert preview.status_code == 200 and 'data-testid="event-preview"' in preview.text
    assert "$25.00" in preview.text and "trend projects about" in preview.text
    bad = post(c, "/admin/events", "/admin/events/preview", EVENT | {"target_date": "2026-10-06"})
    assert bad.status_code == 400 and "days from today" in bad.text
    wrong = post(c, "/admin/events", "/admin/events/publish", EVENT | {"admin_password": "no"})
    assert wrong.status_code == 403
    ok = post(c, "/admin/events", "/admin/events/publish", EVENT | {"admin_password": web.PASSWORD})
    assert ok.headers["location"] == "/admin/events?ok=pool"
    with w.engine.connect() as conn:
        pool = conn.execute(select(Pool.id, Pool.buy_in_cents, Pool.created_by)).one()
    assert pool.buy_in_cents == 2_500 and pool.created_by is not None
    assert "Columbus Day pot" in c.get("/admin/events").text
    void = post(
        c,
        "/admin/events",
        f"/admin/events/{pool.id}/void",
        {"admin_password": web.PASSWORD},
    )
    assert void.headers["location"] == "/admin/events?ok=pool_void"
    again = post(
        c, "/admin/events", f"/admin/events/{pool.id}/void", {"admin_password": web.PASSWORD}
    )
    assert again.status_code == 400 and "already refunded" in again.text
    with w.engine.connect() as conn:
        actions = conn.execute(select(AuditEntry.action)).scalars().all()
    assert "pool.create" in actions and "pool.void" in actions
    flags(w, special_events=False)
    off = post(
        c, "/admin/events", "/admin/events/publish", EVENT | {"admin_password": web.PASSWORD}
    )
    assert off.status_code == 400 and "turned off" in off.text


def test_ai_draft_over_http(
    admin: tuple[TestClient, World], monkeypatch: pytest.MonkeyPatch
) -> None:
    c, w = admin
    store_token(w)
    reply = {
        "title": "Columbus Day pot",
        "question": "Guess the weigh-in on Mon Oct 12.",
        "target_date": "2026-10-12",
        "buy_in_dollars": 25,
    }
    from app.ai import client as ai_client

    real = ai_client.from_store
    monkeypatch.setattr(
        events, "from_store", lambda conn, s, t=None: real(conn, s, canned(reply).transport)
    )
    assert 'data-testid="event-ai-form"' in c.get("/admin/events").text
    drafted = post(c, "/admin/events", "/admin/events/draft", {"text": "Columbus pot $25"})
    assert drafted.status_code == 200 and 'data-testid="event-preview"' in drafted.text
    assert 'name="ai_run_id" value="1"' in drafted.text


def test_season_page_freeze_goal_and_new_season(admin: tuple[TestClient, World]) -> None:
    c, w = admin
    with immediate(w.engine) as conn:
        conn.execute(update(Season).values(start_weight_x10=2216, goal_weight_x10=1500))
    page = c.get("/admin/season").text
    assert "Season 1" in page and 'data-testid="new-season"' not in page
    assert (
        post(c, "/admin/season", "/admin/season/freeze", {"admin_password": "x"}).status_code == 403
    )
    froze = post(c, "/admin/season", "/admin/season/freeze", {"admin_password": web.PASSWORD})
    assert froze.headers["location"] == "/admin/season?ok=instance_frozen"
    assert 'data-testid="frozen-banner"' in c.get("/admin/season").text
    post(c, "/admin/season", "/admin/season/unfreeze", {"admin_password": web.PASSWORD})
    unconfirmed = post(
        c, "/admin/season", "/admin/season/goal", {"confirm": "no", "admin_password": web.PASSWORD}
    )
    assert unconfirmed.status_code == 400
    goal = post(
        c,
        "/admin/season",
        "/admin/season/goal",
        {"confirm": "goal", "admin_password": web.PASSWORD},
    )
    assert goal.headers["location"] == "/admin/season?ok=goal"
    page = c.get("/admin/season").text
    assert "goal reached" in page and 'data-testid="new-season"' in page
    refused = post(c, "/admin/season", "/admin/season/unfreeze", {"admin_password": web.PASSWORD})
    assert refused.status_code == 400 and "new season" in refused.text
    missing = post(
        c,
        "/admin/season",
        "/admin/season/new",
        {"start_weight": "219.4", "goal_weight": "", "admin_password": web.PASSWORD},
    )
    assert missing.status_code == 400
    started = post(
        c,
        "/admin/season",
        "/admin/season/new",
        {"start_weight": "219.4", "goal_weight": "210", "admin_password": web.PASSWORD},
    )
    assert started.headers["location"] == "/admin/season?ok=season"
    with w.engine.connect() as conn:
        rows = conn.execute(select(Season.number, Season.status, Season.goal_weight_x10)).all()
        assert instance.current_state(conn) == instance.ACTIVE
    assert sorted(rows) == [(1, "ended", 1500), (2, "active", 2100)]
    assert (
        post(c, "/admin/season", "/admin/season/nope", {"admin_password": web.PASSWORD}).status_code
        == 404
    )
