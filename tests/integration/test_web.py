import re
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select, update

from app.core.clock import SystemClock
from app.core.config import Settings
from app.core.db import immediate
from app.domain.markets import Timeframe
from app.models import Bet, Market, OddsVersion, OutboxMessage, Selection, User
from app.services import auth, markets
from app.web.main import create_app
from tests.integration import web
from tests.integration.world import World, local, mark_setup_done

HEADERS = {
    "X-Robots-Tag": "noindex, nofollow",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
}


def assert_secure(response: object) -> None:
    headers = response.headers  # type: ignore[attr-defined]
    for key, value in HEADERS.items():
        assert headers[key] == value, key
    csp = headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp and "script-src 'self'" in csp and "unsafe" not in csp


# ---- health, robots, headers -------------------------------------------------------------


def test_healthz_ok_with_noindex_and_security_headers(
    settings: Settings, migrated_engine: Engine
) -> None:
    with TestClient(create_app(settings)) as c:
        response = c.get("/healthz")
    assert response.status_code == 200 and response.json() == {"status": "ok"}
    assert_secure(response)
    assert response.headers["Cache-Control"] == "no-store"


def test_healthz_unavailable_until_migrated(settings: Settings) -> None:
    with TestClient(create_app(settings)) as c:
        response = c.get("/healthz")
    assert response.status_code == 503 and response.json() == {"status": "migrating"}


def test_every_route_and_static_file_sends_security_headers(
    settings: Settings, migrated_engine: Engine
) -> None:
    app = create_app(settings)
    paths = {re.sub(r"\{[^}]+\}", "1", r.path) for r in web.api_routes(app)}
    paths |= {"/static/app.js", "/static/app.css", "/static/vendor/htmx-2.0.11.min.js", "/nope"}
    with web.client(app) as c:
        for path in sorted(paths):
            for method in ("GET", "POST"):
                response = c.request(method, path, follow_redirects=False)
                assert_secure(response)
    assert len(paths) > 12


def test_robots_and_meta_noindex(settings: Settings, migrated_engine: Engine) -> None:
    with web.client(create_app(settings)) as c:
        assert c.get("/robots.txt").text == "User-agent: *\nDisallow: /\n"
        for path in ("/login", "/register"):
            assert '<meta name="robots" content="noindex, nofollow">' in c.get(path).text
        assert "text/css" in c.get("/static/app.css").headers["content-type"]


# ---- auth pages ------------------------------------------------------------------------------


@pytest.fixture
def app_and_code(world: World) -> tuple[TestClient, World, str]:
    code = auth.rotate_registration_code(world.engine, SystemClock())
    app = create_app(world.settings, domain_clock=world.clock)
    return web.client(app), world, code


def test_anonymous_visitors_are_sent_to_login(settings: Settings, migrated_engine: Engine) -> None:
    mark_setup_done(migrated_engine, settings)
    with web.client(create_app(settings)) as c:
        for path in ("/", "/bets/mine", "/bets/feed", "/markets/1", "/admin"):
            response = c.get(path, follow_redirects=False)
            assert (response.status_code, response.headers["location"]) == (303, "/login"), path
        htmx = c.get("/bets/feed", headers={"HX-Request": "true"})
        assert htmx.status_code == 401 and htmx.headers["HX-Redirect"] == "/login"


def test_register_login_logout(app_and_code: tuple[TestClient, World, str]) -> None:
    c, w, code = app_and_code
    with c:
        web.register(c, code)
        cookie = c.cookies.get("wp_session")
        assert cookie
        board = c.get("/")
        assert board.status_code == 200 and "No open daily markets" in board.text
        assert "$1,000.00" in board.text  # starting grant
        set_cookie = c.post(
            "/api/auth/logout",
            data={"csrf_token": web.page_csrf(c)},
            follow_redirects=False,
        )
        assert set_cookie.status_code == 303
        assert c.get("/", follow_redirects=False).headers["location"] == "/login"
        assert auth.resolve(w.engine, SystemClock(), cookie) is None
        web.log_in(c, "player1@example.invalid")
        assert c.get("/").status_code == 200


def test_session_cookie_flags(app_and_code: tuple[TestClient, World, str]) -> None:
    c, _w, code = app_and_code
    with c:
        token = web.form_csrf(c, "/register")
        response = c.post(
            "/api/auth/register",
            data={
                "csrf_token": token,
                "code": code,
                "email": "a@example.invalid",
                "display_name": "A",
                "password": web.PASSWORD,
            },
            follow_redirects=False,
        )
    cookie = next(
        v for k, v in response.headers.multi_items() if k == "set-cookie" and "wp_session" in v
    )
    flags = cookie.lower()
    assert "httponly" in flags and "secure" in flags and "samesite=lax" in flags
    assert "max-age=2592000" in flags  # 30 days for players


def test_form_errors_are_shown(app_and_code: tuple[TestClient, World, str]) -> None:
    c, _w, _code = app_and_code
    with c:
        token = web.form_csrf(c, "/register")
        bad = c.post(
            "/api/auth/register",
            data={
                "csrf_token": token,
                "code": "NOPE",
                "email": "x@example.invalid",
                "display_name": "X",
                "password": web.PASSWORD,
            },
        )
        assert bad.status_code == 400 and "isn&#39;t valid" in bad.text
        token = web.form_csrf(c, "/login")
        wrong = c.post(
            "/api/auth/login",
            data={"csrf_token": token, "email": "x@example.invalid", "password": "wrong-password"},
        )
        assert wrong.status_code == 400 and "wrong" in wrong.text


def test_login_lockout_through_the_form(app_and_code: tuple[TestClient, World, str]) -> None:
    c, _w, code = app_and_code
    with c:
        web.register(c, code)
        c.cookies.clear()
        token = web.form_csrf(c, "/login")
        for _ in range(5):
            c.post(
                "/api/auth/login",
                data={
                    "csrf_token": token,
                    "email": "player1@example.invalid",
                    "password": "not the password",
                },
            )
        locked = c.post(
            "/api/auth/login",
            data={
                "csrf_token": token,
                "email": "player1@example.invalid",
                "password": web.PASSWORD,
            },
        )
        assert locked.status_code == 429


def test_cloudflare_ip_header_is_used_for_limits(
    app_and_code: tuple[TestClient, World, str],
) -> None:
    c, _w, _code = app_and_code
    with c:
        token = web.form_csrf(c, "/login")
        for n in range(30):
            c.post(
                "/api/auth/login",
                data={"csrf_token": token, "email": f"n{n}@example.invalid", "password": "x" * 10},
                headers={"CF-Connecting-IP": "203.0.113.9"},
            )
        blocked = c.post(
            "/api/auth/login",
            data={"csrf_token": token, "email": "z@example.invalid", "password": "x" * 10},
            headers={"CF-Connecting-IP": "203.0.113.9"},
        )
        other = c.post(
            "/api/auth/login",
            data={"csrf_token": token, "email": "z@example.invalid", "password": "x" * 10},
            headers={"CF-Connecting-IP": "203.0.113.10"},
        )
    assert blocked.status_code == 429 and other.status_code == 400


# ---- CSRF and roles -----------------------------------------------------------------------------


def test_csrf_is_required_on_every_post(app_and_code: tuple[TestClient, World, str]) -> None:
    c, w, code = app_and_code
    with c:
        web.register(c, code)
        for path in ("/api/auth/login", "/api/auth/register", "/api/auth/logout", "/api/bets"):
            assert c.post(path, data={"x": "1"}).status_code == 403, path
            assert c.post(path, headers={"X-CSRF-Token": "forged"}).status_code == 403, path
        # The form token from another browser doesn't work either.
        other = web.client(create_app(w.settings, domain_clock=w.clock))
        with other:
            stolen = web.form_csrf(other, "/login")
        assert c.post("/api/auth/logout", data={"csrf_token": stolen}).status_code == 403
        assert c.get("/").status_code == 200  # still logged in


def test_roles(app_and_code: tuple[TestClient, World, str]) -> None:
    c, w, code = app_and_code
    with c:
        web.register(c, code)
        assert c.get("/admin").status_code == 403
    admin = web.client(create_app(w.settings, domain_clock=w.clock))
    with admin:
        web.as_admin(admin, w.engine)
        assert admin.get("/", follow_redirects=False).headers["location"] == "/admin"
        assert admin.get("/admin").status_code == 200
        assert admin.get("/bets/mine").status_code == 403
        bet = admin.post(
            "/api/bets", json={}, headers={"X-CSRF-Token": web.page_csrf(admin, "/admin")}
        )
        assert bet.status_code == 403


def test_dev_routes_need_admin_and_csrf(settings: Settings, migrated_engine: Engine) -> None:
    dev = settings.model_copy(update={"data_provider": "simulated"})
    mark_setup_done(migrated_engine, dev)
    with web.client(create_app(dev)) as c:
        assert c.post("/dev/clock/advance", data={"days": "1"}).status_code == 403  # no CSRF
        web.as_admin(c, migrated_engine)
        assert c.post("/dev/clock/advance", data={"days": "1"}).status_code == 403
        ok = c.post(
            "/dev/clock/advance",
            data={"days": "0", "hours": "1", "csrf_token": web.page_csrf(c, "/dev/clock")},
            follow_redirects=False,
        )
        assert ok.status_code == 303


# ---- board and bets ------------------------------------------------------------------------------


def board_with_markets(app_and_code: tuple[TestClient, World, str]) -> tuple[TestClient, World]:
    """Weekly markets dropped Sun Oct 4 18:00, daily Mon Oct 5 11:00; clock at Mon noon."""
    c, w, code = app_and_code
    w.clock.set(local(2026, 10, 4, 18))
    markets.drop(w.engine, w.clock, w.config, Timeframe.WEEKLY, date(2026, 10, 4))
    w.clock.set(local(2026, 10, 5, 12))
    markets.drop(w.engine, w.clock, w.config, Timeframe.DAILY, date(2026, 10, 5))
    c.__enter__()
    web.register(c, code)
    return c, w


def weight_pick(w: World) -> tuple[int, int, int | None]:
    with w.engine.connect() as conn:
        row = conn.execute(
            select(Selection.id, OddsVersion.id, OddsVersion.odds)
            .join(
                OddsVersion, (OddsVersion.market_id == Selection.market_id) & OddsVersion.is_current
            )
            .join(Market, Market.id == Selection.market_id)
            .where(Selection.side == "over", Market.timeframe == "daily", Market.metric == "weight")
            .order_by(Selection.id)
            .limit(1)
        ).one()
    return row[0], row[1], row[2]["over"]


def test_board_tabs(app_and_code: tuple[TestClient, World, str]) -> None:
    c, _w = board_with_markets(app_and_code)
    daily = c.get("/")
    assert daily.text.count('class="market"') == 5
    assert 'x-data="slip"' in daily.text and 'data-testid="slip"' in daily.text
    weekly = c.get("/?tab=weekly", headers={"HX-Request": "true"})
    assert "<html" not in weekly.text  # partial
    assert "No open weekly markets" in weekly.text  # weekly locked Sunday night (D-031)
    assert "No open monthly markets" in c.get("/?tab=monthly").text
    c.__exit__(None, None, None)


def test_place_bet_through_the_api(app_and_code: tuple[TestClient, World, str]) -> None:
    c, w = board_with_markets(app_and_code)
    sel, ver, _ = weight_pick(w)
    csrf = web.page_csrf(c)
    body = {"selection_id": sel, "odds_version_id": ver, "stake_cents": 2_500, "client_key": "k-1"}
    placed = c.post("/api/bets", json=body, headers={"X-CSRF-Token": csrf})
    assert placed.status_code == 200 and placed.json()["ok"] is True
    again = c.post("/api/bets", json=body, headers={"X-CSRF-Token": csrf})
    assert again.json()["replayed"] is True
    too_big = c.post(
        "/api/bets",
        json=body | {"stake_cents": 10**9, "client_key": "k-2"},
        headers={"X-CSRF-Token": csrf},
    )
    assert too_big.status_code == 409 and too_big.json()["reason"] == "insufficient_funds"
    junk = c.post("/api/bets", json={"selection_id": "x"}, headers={"X-CSRF-Token": csrf})
    assert junk.status_code == 400
    with w.engine.connect() as conn:
        assert len(conn.execute(select(Bet.id)).all()) == 1
        assert conn.execute(
            select(OutboxMessage.category).where(OutboxMessage.category != "new_markets")
        ).scalars().all() == ["bets_placed"]
    mine = c.get("/bets/mine")
    assert "$25.00" in mine.text and "$975.00" in mine.text
    feed = c.get("/bets/feed")
    assert "Player 1" in feed.text and "player1@example.invalid" not in feed.text
    with w.engine.connect() as conn:
        market_id = conn.execute(
            select(Selection.market_id).where(Selection.id == sel)
        ).scalar_one()
    market_page = c.get(f"/markets/{market_id}")
    assert market_page.status_code == 200 and "Player 1" in market_page.text
    assert c.get("/markets/99999").status_code == 404
    w.clock.set(local(2026, 10, 5, 22, 0))
    late = c.post("/api/bets", json=body | {"client_key": "k-3"}, headers={"X-CSRF-Token": csrf})
    assert late.json()["reason"] == "locked" and "locked" in late.json()["message"]
    c.__exit__(None, None, None)


def test_frozen_player_sees_the_board_but_cannot_bet(
    app_and_code: tuple[TestClient, World, str],
) -> None:
    c, w = board_with_markets(app_and_code)
    with immediate(w.engine) as conn:
        conn.execute(update(User).values(status="frozen"))
    sel, ver, _ = weight_pick(w)
    response = c.post(
        "/api/bets",
        json={"selection_id": sel, "odds_version_id": ver, "stake_cents": 500, "client_key": "f"},
        headers={"X-CSRF-Token": web.page_csrf(c)},
    )
    assert response.json()["reason"] == "user_inactive"
    c.__exit__(None, None, None)
