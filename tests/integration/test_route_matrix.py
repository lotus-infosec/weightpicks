"""Route review as a test: every route the app registers, checked for
authentication, roles and CSRF. A new route that forgets any of these fails here."""

import re
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.clock import SystemClock
from app.models import Bet
from app.services import auth
from app.web.main import create_app
from app.web.security import CSRF_COOKIE
from tests.integration import web
from tests.integration.test_web import board_with_markets, weight_pick
from tests.integration.world import World

# Reachable without a session. Everything else must send anonymous visitors to /login.
PUBLIC = {
    "/healthz",
    "/robots.txt",
    "/static/app.css",
    "/brand/{name}",
    "/manifest.webmanifest",
    "/login",
    "/register",
    "/reset",
    "/reset/{token}",
    "/api/auth/login",
    "/api/auth/register",
    "/api/auth/reset",
    "/api/auth/reset/complete",
    "/api/auth/logout",
}
SETUP = re.compile(r"^/setup(/|$)")  # 404 once setup is done (checked in test_setup)


def concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "1", path)


@pytest.fixture
def clients(world: World) -> Iterator[tuple[TestClient, TestClient, TestClient]]:
    anon, player, admin = (
        web.client(create_app(world.settings, domain_clock=world.clock)) for _ in range(3)
    )
    code = auth.rotate_registration_code(world.engine, SystemClock())
    with anon, player, admin:
        web.register(player, code)
        web.as_admin(admin, world.engine)
        yield anon, player, admin


def all_routes(c: TestClient) -> list[tuple[str, str]]:
    found = []
    for route in web.api_routes(c.app):  # type: ignore[arg-type]
        if SETUP.match(route.path):
            continue
        found += [(m, route.path) for m in sorted(route.methods or ()) if m != "HEAD"]
    assert len(found) > 70
    return found


def test_every_private_route_needs_a_session(
    clients: tuple[TestClient, TestClient, TestClient],
) -> None:
    anon, _player, _admin = clients
    cookie_token = web.form_csrf(anon, "/login")  # a valid pre-login token: CSRF passes
    for method, path in all_routes(anon):
        if path in PUBLIC:
            continue
        data = {"csrf_token": cookie_token} if method == "POST" else None
        r = anon.request(method, concrete(path), data=data, follow_redirects=False)
        assert r.status_code in (303, 401, 403), (method, path, r.status_code)
        if r.status_code == 303:
            assert r.headers["location"] == "/login", (method, path)


def test_roles_are_enforced_everywhere(
    clients: tuple[TestClient, TestClient, TestClient],
) -> None:
    _anon, player, admin = clients
    player_token, admin_token = web.page_csrf(player), web.page_csrf(admin, "/admin")
    for method, path in all_routes(player):
        if path in PUBLIC:
            continue
        admin_only = path.startswith(("/admin", "/dev"))
        c, token = (player, player_token) if admin_only else (admin, admin_token)
        data = {"csrf_token": token} if method == "POST" else None
        r = c.request(method, concrete(path), data=data, follow_redirects=False)
        if path == "/":  # the board sends the admin to their own home
            assert r.headers["location"] == "/admin"
            continue
        assert r.status_code == 403, (method, path, r.status_code)


def test_every_unsafe_route_needs_its_own_sessions_csrf_token(
    clients: tuple[TestClient, TestClient, TestClient],
) -> None:
    anon, player, admin = clients
    other_session = web.page_csrf(player)
    admin.cookies.set(CSRF_COOKIE, "planted", domain="testserver.local")
    posts = [p for m, p in all_routes(admin) if m == "POST"]
    assert len(posts) >= 40
    for path in posts:
        for c in (anon, player, admin):
            for data in ({}, {"csrf_token": "planted"}, {"csrf_token": other_session}):
                if c is player and data.get("csrf_token") == other_session:
                    continue  # that one is the player's own token
                r = c.post(concrete(path), data=data, follow_redirects=False)
                assert r.status_code == 403, (path, data, r.status_code)


def test_bets_are_placed_for_the_session_user_only(world: World) -> None:
    """IDOR: the bet API takes the user from the session; a user_id in the body is ignored,
    and nobody else's bets show on /bets/mine."""
    code = auth.rotate_registration_code(world.engine, SystemClock())
    one = web.client(create_app(world.settings, domain_clock=world.clock))
    two = web.client(create_app(world.settings, domain_clock=world.clock))
    board_with_markets((one, world, code))
    with two:
        web.register(two, code, 2)
        selection_id, odds_version_id, _ = weight_pick(world)
        r = one.post(
            "/api/bets",
            json={
                "selection_id": selection_id,
                "odds_version_id": odds_version_id,
                "stake_cents": 500,
                "client_key": "k-idor-1",
                "user_id": 2,
            },
            headers={"X-CSRF-Token": web.page_csrf(one)},
        )
        assert r.status_code == 200, r.text
        with world.engine.connect() as conn:
            owner = conn.execute(select(Bet.user_id).where(Bet.id == r.json()["bet_id"]))
            assert owner.scalar_one() != 2
        assert "$5.00" not in two.get("/bets/mine").text
    one.__exit__(None, None, None)
