"""Admin AI pages: review queue, publishing mode, run-now, AI runs, notes, Workers AI
settings; the AI blurb on the board (D-042)."""

from collections.abc import Iterator
from datetime import date, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select, update

from app.core.clock import SimClock, SystemClock
from app.core.db import immediate
from app.domain.markets import Timeframe
from app.models import AdminNote, AiProposal, AuditEntry, Command, InstanceSettingsRow, Market
from app.services import ai_props, auth, markets, secrets
from app.web.main import create_app
from tests.fake_ai import canned
from tests.integration import web
from tests.integration.world import World, local

KEY = "k" * 40
TOKEN = "fake" * 6  # a placeholder, not a credential


@pytest.fixture
def admin(world: World) -> Iterator[tuple[TestClient, World]]:
    markets.drop(world.engine, world.clock, world.config, Timeframe.DAILY, date(2026, 10, 5))
    world.settings = world.settings.model_copy(update={"app_secret_key": SecretStr(KEY)})
    c = web.client(create_app(world.settings, domain_clock=world.clock))
    c.__enter__()
    web.as_admin(c, world.engine)
    with immediate(world.engine) as conn:
        flags = conn.execute(select(InstanceSettingsRow.flags)).scalar_one() or {}
        conn.execute(
            update(InstanceSettingsRow).values(
                flags=dict(flags) | {"props_futures": True, "ai_props": True}
            )
        )
    yield c, world
    c.__exit__(None, None, None)


def post(c: TestClient, page: str, action: str, data: dict[str, str] | None = None) -> Any:
    token = web.page_csrf(c, page)
    return c.post(action, data={"csrf_token": token, **(data or {})}, follow_redirects=False)


def audit(w: World) -> list[tuple[str, Any]]:
    with w.engine.connect() as conn:
        return [tuple(r) for r in conn.execute(select(AuditEntry.action, AuditEntry.after))]


def queue_one(w: World) -> int:
    """Store a token and run one cycle with a canned, valid reply."""
    with immediate(w.engine) as conn:
        secrets.put(conn, w.clock, KEY, "workers_ai.account_id", "acct")
        secrets.put(conn, w.clock, KEY, "workers_ai.token", "tok")
    reply = {
        "proposals": [
            {
                "template": "beat_last_week",
                "params": {"metric": "steps"},
                "title": "Better than last week?",
                "blurb": "Last week set the bar for steps.",
            }
        ]
    }
    report = ai_props.run(
        w.engine,
        w.clock,
        SimClock(local(2026, 10, 5, 12)),
        w.settings,
        "props_manual",
        transport=canned(reply).transport,
    )
    (proposal_id,) = report.queued
    return proposal_id


def test_workers_ai_settings_card(admin: tuple[TestClient, World]) -> None:
    c, w = admin
    page = c.get("/admin/discord").text
    assert 'data-testid="ai-configured">not set' in page
    form = {"account_id": "acct-1", "token": TOKEN}
    wrong = post(c, "/admin/discord", "/admin/discord/workers-ai", form | {"admin_password": "no"})
    assert wrong.status_code == 403
    with w.engine.connect() as conn:
        assert "workers_ai.token" not in secrets.names(conn)
    ok = post(
        c, "/admin/discord", "/admin/discord/workers-ai", form | {"admin_password": web.PASSWORD}
    )
    assert ok.headers["location"] == "/admin/discord?ok=workers_ai"
    page = c.get("/admin/discord?ok=workers_ai").text
    assert 'data-testid="ai-configured">configured' in page and TOKEN not in page
    with w.engine.connect() as conn:
        assert secrets.get(conn, KEY, "workers_ai.token") == TOKEN
    assert all(TOKEN not in str(after) for _, after in audit(w))
    blank = post(c, "/admin/discord", "/admin/discord/workers-ai", {"admin_password": web.PASSWORD})
    assert blank.status_code == 400
    clear = post(
        c,
        "/admin/discord",
        "/admin/discord/workers-ai",
        {"clear": "on", "admin_password": web.PASSWORD},
    )
    assert clear.status_code == 303
    with w.engine.connect() as conn:
        assert not {"workers_ai.token", "workers_ai.account_id"} & set(secrets.names(conn))


def test_ai_flags_on_settings(admin: tuple[TestClient, World]) -> None:
    c, w = admin
    post(c, "/admin/discord", "/admin/discord/flags", {"ai_hype": "on", "props_futures": "on"})
    with w.engine.connect() as conn:
        flags = conn.execute(select(InstanceSettingsRow.flags)).scalar_one()
    assert flags["ai_hype"] is True and flags["ai_props"] is False


def test_review_queue_approve_reject(admin: tuple[TestClient, World]) -> None:
    c, w = admin
    proposal_id = queue_one(w)
    page = c.get("/admin/props").text
    assert 'data-testid="proposal"' in page and "Last week set the bar for steps." in page
    assert "fair chance" in page
    ok = post(c, "/admin/props", f"/admin/props/ai/{proposal_id}/approve")
    assert ok.headers["location"] == "/admin/props?ok=approved"
    with w.engine.connect() as conn:
        m = conn.execute(select(Market).where(Market.origin == "ai")).one()
    assert m.blurb == "Last week set the bar for steps."
    again = post(c, "/admin/props", f"/admin/props/ai/{proposal_id}/approve")
    assert again.status_code == 409 and "no longer waiting" in again.text
    # Players see the blurb under the code-built title, on the board and the market page.
    player = web.client(create_app(w.settings, domain_clock=w.clock))
    with player:
        web.register(player, auth.rotate_registration_code(w.engine, SystemClock()))
        board = player.get("/?tab=prop").text
        page = player.get(f"/markets/{m.id}").text
    assert m.title in board and 'data-testid="blurb">Last week set the bar for steps.' in board
    assert 'class="muted blurb">Last week set the bar for steps.' in page
    with immediate(w.engine) as conn:
        conn.execute(update(AiProposal).values(status="pending", market_id=None))
        conn.execute(update(AiProposal).values(dedupe_key="other"))
    rejected = post(c, "/admin/props", f"/admin/props/ai/{proposal_id}/reject")
    assert rejected.headers["location"] == "/admin/props?ok=rejected"
    assert [a for a, _ in audit(w)][-2:] == ["ai_proposal.approve", "ai_proposal.reject"]


def test_mode_needs_password_and_run_now_queues_a_command(admin: tuple[TestClient, World]) -> None:
    c, w = admin
    bad = post(
        c, "/admin/props", "/admin/props/ai/mode", {"ai_mode": "auto", "admin_password": "x"}
    )
    assert bad.status_code == 403
    good = post(
        c,
        "/admin/props",
        "/admin/props/ai/mode",
        {"ai_mode": "auto", "admin_password": web.PASSWORD},
    )
    assert good.headers["location"] == "/admin/props?ok=ai_mode"
    assert "Auto-publish is on" in c.get("/admin/props").text
    nonsense = post(
        c,
        "/admin/props",
        "/admin/props/ai/mode",
        {"ai_mode": "yolo", "admin_password": web.PASSWORD},
    )
    assert nonsense.status_code == 400
    assert post(c, "/admin/props", "/admin/props/ai/run").status_code == 303
    assert post(c, "/admin/props", "/admin/props/ai/run").status_code == 303  # still one pending
    with w.engine.connect() as conn:
        commands = conn.execute(select(Command.type, Command.status)).all()
    assert commands == [("ai_props_now", "pending")]
    assert ("settings.ai_mode", {"ai_mode": "auto"}) in audit(w)


def test_ai_runs_page(admin: tuple[TestClient, World]) -> None:
    c, w = admin
    assert 'data-testid="ai-runs-empty"' in c.get("/admin/ai").text
    queue_one(w)
    page = c.get("/admin/ai").text
    assert "props manual" in page and "props_v1" in page and "of 5,000 neurons" in page
    assert "Raw output" in page


def test_notes(admin: tuple[TestClient, World]) -> None:
    c, w = admin
    today = date(2026, 10, 5)
    ok = post(
        c,
        "/admin/notes",
        "/admin/notes",
        {"text": "Traveling Thu-Sun", "active_from": "2026-10-08", "active_to": "2026-10-11"},
    )
    assert ok.headers["location"] == "/admin/notes?ok=note"
    post(
        c,
        "/admin/notes",
        "/admin/notes",
        {"text": "Tailgate", "active_from": "2026-10-05", "active_to": "2026-10-05"},
    )
    page = c.get("/admin/notes").text
    assert "Traveling Thu-Sun" in page and "upcoming" in page and "active" in page
    for bad in (
        {"text": "", "active_from": "2026-10-05", "active_to": "2026-10-05"},
        {"text": "x", "active_from": "2026-10-06", "active_to": "2026-10-05"},
        {"text": "x", "active_from": "2026-10-05", "active_to": "2027-01-05"},
        {"text": "x", "active_from": "soon", "active_to": "2026-10-05"},
    ):
        assert post(c, "/admin/notes", "/admin/notes", bad).status_code == 400
    with w.engine.connect() as conn:
        notes = {n.text: n.id for n in conn.execute(select(AdminNote))}
    assert post(c, "/admin/notes", f"/admin/notes/{notes['Tailgate']}/end").status_code == 303
    assert (
        post(c, "/admin/notes", f"/admin/notes/{notes['Traveling Thu-Sun']}/end").status_code == 303
    )
    assert post(c, "/admin/notes", "/admin/notes/999/end").status_code == 404
    with w.engine.connect() as conn:
        ends = {n.text: n.active_to for n in conn.execute(select(AdminNote))}
    assert ends == {
        "Tailgate": today - timedelta(days=1),
        "Traveling Thu-Sun": date(2026, 10, 7),  # never starts
    }
    assert [a for a, _ in audit(w)].count("note.end") == 2
