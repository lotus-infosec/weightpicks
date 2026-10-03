import re
from datetime import date
from typing import Any

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.clock import SystemClock
from app.domain.markets import Timeframe
from app.models import AuditEntry, Command, Market, User
from app.services import auth, markets
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


def admin_routes(app: object) -> set[tuple[str, str]]:
    def walk(routes: list[object]) -> list[APIRoute]:
        found: list[APIRoute] = []
        for r in routes:
            if isinstance(r, APIRoute):
                found.append(r)
            elif hasattr(r, "original_router"):
                found += walk(r.original_router.routes)
        return found

    actions = {
        "/admin/users/{user_id}/{action}": ("freeze", "unfreeze", "ban", "reset-password"),
        "/admin/bank/{action}": ("bailout", "adjust", "economy"),
    }
    found: set[tuple[str, str]] = set()
    for r in walk(list(app.routes)):  # type: ignore[attr-defined]
        if not r.path.startswith("/admin"):
            continue
        for action in actions.get(r.path, (None,)):
            path = r.path.replace("{action}", action) if action else r.path
            found |= {(m, re.sub(r"\{[^}]+\}", "1", path)) for m in r.methods or set()}
    return found


def test_permission_matrix(clients: tuple[TestClient, TestClient, World]) -> None:
    admin_c, player_c, w = clients
    routes = admin_routes(admin_c.app)
    assert len(routes) == 18
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
