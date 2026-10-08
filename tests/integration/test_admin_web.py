import re
from datetime import date
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select, update

from app.core.clock import SystemClock
from app.core.db import immediate
from app.domain.markets import Timeframe
from app.models import AuditEntry, Command, InstanceSettingsRow, Market, OutboxMessage, User
from app.services import admin, audit, auth, instance, markets, secrets
from app.services.sync import record_failure
from app.web.main import create_app
from tests.integration import web
from tests.integration.test_bets_settlement import account
from tests.integration.world import World


@pytest.fixture
def clients(world: World) -> tuple[TestClient, TestClient, World]:
    """An admin client and a player client against the same instance with markets."""
    markets.drop(world.engine, world.clock, world.config, Timeframe.DAILY, date(2026, 10, 5))
    admin_client = web.client(create_app(world.settings, domain_clock=world.clock))
    admin_client.__enter__()
    web.as_admin(admin_client, world.engine)
    code = auth.rotate_registration_code(world.engine, SystemClock())
    player_client = web.client(create_app(world.settings, domain_clock=world.clock))
    player_client.__enter__()
    web.register(player_client, code)
    return admin_client, player_client, world


def admin_routes(app: FastAPI) -> set[tuple[str, str]]:
    actions = {
        "/admin/users/{user_id}/{action}": ("freeze", "unfreeze", "ban", "reset-password"),
        "/admin/bank/{action}": ("bailout", "adjust", "economy"),
    }
    found: set[tuple[str, str]] = set()
    for r in web.api_routes(app):
        if not r.path.startswith("/admin"):
            continue
        for action in actions.get(r.path, (None,)):
            path = r.path.replace("{action}", action) if action else r.path
            found |= {(m, re.sub(r"\{[^}]+\}", "1", path)) for m in r.methods or set()}
    return found


def test_permission_matrix(clients: tuple[TestClient, TestClient, World]) -> None:
    admin_c, player_c, w = clients
    routes = admin_routes(admin_c.app)  # type: ignore[arg-type]
    assert len(routes) == 56  # STAGE13 +9 (AI); STAGE14 +7; STAGE15 +12; #17/#30 +3 (instance)
    anon = web.client(create_app(w.settings, domain_clock=w.clock))
    with anon:
        for method, path in sorted(routes):
            as_player = player_c.request(
                method,
                path,
                data={"csrf_token": web.page_csrf(player_c)} if method == "POST" else None,
                follow_redirects=False,
            )
            assert as_player.status_code == 403, (method, path, as_player.status_code)
            as_anon = anon.request(method, path, follow_redirects=False)
            assert as_anon.status_code in (303, 401, 403), (method, path)
            if method == "GET":
                assert as_anon.headers.get("location") == "/login"
        for method, path in sorted(routes):
            if method == "GET":
                assert admin_c.get(path).status_code in (200, 404), path
    admin_c.__exit__(None, None, None)
    player_c.__exit__(None, None, None)


def post(c: TestClient, page: str, action: str, data: dict[str, str]) -> Any:
    return c.post(
        action, data={"csrf_token": web.page_csrf(c, page), **data}, follow_redirects=False
    )


def test_void_needs_the_password(clients: tuple[TestClient, TestClient, World]) -> None:
    admin_c, _p, w = clients
    with w.engine.connect() as conn:
        market_id = conn.execute(select(Market.id).where(Market.metric == "weight")).scalar_one()
    page = f"/admin/markets/{market_id}"
    wrong = post(admin_c, page, f"{page}/void", {"reason": "test", "admin_password": "nope"})
    assert wrong.status_code == 200 and "not your password" in wrong.text
    ok = post(admin_c, page, f"{page}/void", {"reason": "bad line", "admin_password": web.PASSWORD})
    assert ok.headers["location"] == f"{page}?ok=voided"
    assert "Market voided" in admin_c.get(f"{page}?ok=voided").text
    with w.engine.connect() as conn:
        assert (
            conn.execute(select(Market.status).where(Market.id == market_id)).scalar_one()
            == "voided"
        )
        actions = conn.execute(select(AuditEntry.action).order_by(AuditEntry.id)).scalars().all()
    assert actions == ["reauth_failed", "market.void"]
    admin_c.__exit__(None, None, None)


def test_user_actions_through_the_ui(clients: tuple[TestClient, TestClient, World]) -> None:
    admin_c, player_c, w = clients
    with w.engine.connect() as conn:
        user_id = conn.execute(select(User.id).where(User.role == "player")).scalar_one()
    page = f"/admin/users/{user_id}"
    assert "Player 1" in admin_c.get("/admin/users").text
    assert post(admin_c, page, f"{page}/freeze", {}).headers["location"] == f"{page}?ok=frozen"
    reset = post(admin_c, page, f"{page}/reset-password", {"admin_password": web.PASSWORD})
    temporary = re.search(r'data-testid="temporary-password">([^<]+)<', reset.text)
    assert temporary and len(temporary.group(1)) == 12
    assert (
        player_c.get("/", follow_redirects=False).headers["location"] == "/login"
    )  # sessions ended
    banned = post(admin_c, page, f"{page}/ban", {"reason": "spam", "admin_password": web.PASSWORD})
    assert banned.headers["location"] == f"{page}?ok=banned"
    with w.engine.connect() as conn:
        assert conn.execute(select(User.status).where(User.id == user_id)).scalar_one() == "banned"
    assert "Remove (ban)" not in admin_c.get(page).text
    admin_c.__exit__(None, None, None)
    player_c.__exit__(None, None, None)


def test_bank_and_registration_pages(clients: tuple[TestClient, TestClient, World]) -> None:
    admin_c, _p, w = clients
    with w.engine.connect() as conn:
        user_id = conn.execute(select(User.id).where(User.role == "player")).scalar_one()
    adjusted = post(
        admin_c,
        "/admin/bank",
        "/admin/bank/adjust",
        {
            "user_id": str(user_id),
            "amount": "-100.50",
            "reason": "fix",
            "admin_password": web.PASSWORD,
        },
    )
    assert adjusted.headers["location"] == "/admin/bank?ok=adjusted"
    assert account(w, user_id)[0] == 100_000 - 10_050
    bad = post(
        admin_c,
        "/admin/bank",
        "/admin/bank/adjust",
        {"user_id": str(user_id), "amount": "lots", "reason": "x", "admin_password": web.PASSWORD},
    )
    assert bad.status_code == 400
    not_bust = post(
        admin_c,
        "/admin/bank",
        "/admin/bank/bailout",
        {"user_id": str(user_id), "admin_password": web.PASSWORD},
    )
    assert "only for players who are bust" in not_bust.text
    economy = {
        "starting_bankroll": "1500",
        "daily_allowance": "50",
        "bailout": "500",
        "bailout_cooldown_days": "2",
        "vig": "-110",
        "max_bet": "",
        "max_parlay_legs": "6",
        "high_roller": "500",
        "admin_password": web.PASSWORD,
    }
    assert (
        post(admin_c, "/admin/bank", "/admin/bank/economy", economy).headers["location"]
        == "/admin/bank?ok=economy"
    )
    invalid = post(admin_c, "/admin/bank", "/admin/bank/economy", economy | {"vig": "-500"})
    assert invalid.status_code == 400
    rotated = post(
        admin_c, "/admin/registration", "/admin/registration", {"admin_password": web.PASSWORD}
    )
    assert re.search(r'registration-code">[A-Z0-9]{4}-', rotated.text)
    audit = admin_c.get("/admin/audit").text
    for action in ("bank.adjust", "settings.economy", "registration.rotate"):
        assert action in audit
    assert web.PASSWORD not in audit
    admin_c.__exit__(None, None, None)


def test_sync_now_button_and_dashboard(clients: tuple[TestClient, TestClient, World]) -> None:
    admin_c, player_c, w = clients
    dash = admin_c.get("/admin")
    assert "Dashboard" in dash.text and "Sync now" in dash.text
    assert post(admin_c, "/admin", "/admin/sync", {}).headers["location"] == "/admin?ok=sync"
    with w.engine.connect() as conn:
        assert conn.execute(select(Command.type, Command.status)).one() == ("sync_now", "pending")
    assert "Last request: pending" in admin_c.get("/admin").text
    admin_c.__exit__(None, None, None)
    player_c.__exit__(None, None, None)


def test_dashboard_shows_why_sync_failed(clients: tuple[TestClient, TestClient, World]) -> None:
    admin_c, player_c, w = clients
    record_failure(w.engine, w.clock, "garmindb", "Garmin login failed: rerun garmin-login")
    page = admin_c.get("/admin").text
    assert "Garmin login failed: rerun garmin-login" in page and "nothing settles" in page
    admin_c.__exit__(None, None, None)
    player_c.__exit__(None, None, None)


HOOK = "https://discord.com/api/webhooks/123456/secret-token_value"


def test_discord_page_webhooks_tests_and_flags(
    clients: tuple[TestClient, TestClient, World],
) -> None:
    admin_c, player_c, w = clients
    keyed = w.settings.model_copy(update={"app_secret_key": SecretStr("k" * 48)})
    admin_c.app.state.settings = keyed  # type: ignore[attr-defined]
    page = "/admin/discord"
    assert 'data-testid="hook-busts">off' in admin_c.get(page).text
    wrong = post(
        admin_c,
        page,
        f"{page}/webhook",
        {"category": "busts", "url": HOOK, "admin_password": "nope"},
    )
    assert wrong.status_code == 403
    bad = post(
        admin_c,
        page,
        f"{page}/webhook",
        {"category": "busts", "url": "https://x.test", "admin_password": web.PASSWORD},
    )
    assert bad.status_code == 400
    ok = post(
        admin_c,
        page,
        f"{page}/webhook",
        {"category": "busts", "url": HOOK, "admin_password": web.PASSWORD},
    )
    assert ok.headers["location"] == f"{page}?ok=webhook"
    text = admin_c.get(page).text
    assert 'data-testid="hook-busts">set' in text and "secret-token_value" not in text
    with w.engine.connect() as conn:
        stored = secrets.get(conn, "k" * 48, "webhook.busts")
        audit_rows = conn.execute(select(AuditEntry.action, AuditEntry.after)).all()
    assert stored == HOOK
    assert "secret-token_value" not in str(audit_rows)
    assert ("discord.webhook_set", {"category": "busts", "set": True}) in audit_rows
    # Test button queues a test post for that category.
    assert post(admin_c, page, f"{page}/test/busts", {}).headers["location"] == f"{page}?ok=test"
    with w.engine.connect() as conn:
        test_row = conn.execute(
            select(OutboxMessage.category, OutboxMessage.payload).where(
                OutboxMessage.dedupe_key.like("test:%")
            )
        ).one()
    assert test_row == ("busts", {"kind": "test"})
    # Flags: both on, then registration closed again.
    post(admin_c, page, f"{page}/flags", {"discord_public": "on", "registration_open": "on"})
    post(admin_c, page, f"{page}/flags", {"discord_public": "on"})
    with w.engine.connect() as conn:
        flags = instance.read(conn).flags  # type: ignore[union-attr]
    assert flags["discord_public"] and not flags["registration_open"]
    # Clearing a webhook turns the category off.
    post(
        admin_c,
        page,
        f"{page}/webhook",
        {"category": "busts", "url": "", "admin_password": web.PASSWORD},
    )
    assert 'data-testid="hook-busts">off' in admin_c.get(page).text
    admin_c.__exit__(None, None, None)
    player_c.__exit__(None, None, None)


def test_register_page_is_closed_when_the_flag_is_off(world: World) -> None:
    with immediate(world.engine) as conn:
        conn.execute(update(InstanceSettingsRow).values(flags={"registration_open": False}))
    c = web.client(create_app(world.settings, domain_clock=world.clock))
    with c:
        page = c.get("/register").text
        assert 'data-testid="invite-closed"' in page and 'name="code"' not in page
        meta = c.get("/login").text
        token = re.search(r'name="csrf_token" value="([^"]+)"', meta)
        assert token
        refused = c.post(
            "/api/auth/register",
            data={
                "csrf_token": token.group(1),
                "code": "X",
                "email": "a@b.invalid",
                "display_name": "A",
                "password": "x" * 12,
            },
        )
        assert refused.status_code == 403 and 'data-testid="invite-closed"' in refused.text


def test_leaderboard_page(clients: tuple[TestClient, TestClient, World]) -> None:
    admin_c, player_c, w = clients
    code = auth.rotate_registration_code(w.engine, SystemClock())
    other = web.client(create_app(w.settings, domain_clock=w.clock))
    with other:
        web.register(other, code, n=2)
    page = player_c.get("/leaderboard").text
    assert 'data-testid="leaderboard"' in page
    assert "Player 1" in page and "Player 2" in page and ">you<" in page
    with w.engine.connect() as conn:
        second = conn.execute(select(User.id).where(User.display_name == "Player 2")).scalar_one()
    admin.ban(w.engine, w.clock, audit.Actor(user_id=1), second, "test")
    page = player_c.get("/leaderboard").text
    assert "Player 2" not in page  # removed players are hidden (C5)
    assert player_c.get("/leaderboard?season=999").status_code == 200  # unknown -> current
    admin_c.__exit__(None, None, None)
    player_c.__exit__(None, None, None)
