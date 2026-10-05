"""SMTP settings, test email and password reset by email (STAGE15), against a local
fake SMTP server; and the hidden-when-off behaviour."""

import re
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select

from app.core.clock import SimClock, SystemClock
from app.models import OutboxMessage, PasswordReset, Session
from app.notify.dispatcher import Dispatcher
from app.services import auth, password_reset
from app.web.main import create_app
from tests.fake_smtp import FakeSmtp
from tests.integration import web
from tests.integration.world import World

KEY = "k" * 40
PLAYER = "player@example.invalid"


@pytest.fixture
def smtp() -> Iterator[FakeSmtp]:
    server = FakeSmtp()
    yield server
    server.close()


@pytest.fixture
def site(world: World) -> Iterator[tuple[TestClient, World, SimClock]]:
    world.settings = world.settings.model_copy(
        update={"app_secret_key": SecretStr(KEY), "wp_base_url": "https://wp.example"}
    )
    real = SimClock(SystemClock().now())  # sessions and resets run on real time
    c = web.client(create_app(world.settings, auth_clock=real, domain_clock=world.clock))
    c.__enter__()
    web.as_admin(c, world.engine)
    yield c, world, real
    c.__exit__(None, None, None)


def post(c: TestClient, page: str, action: str, data: dict[str, str]) -> Any:
    return c.post(
        action, data={"csrf_token": web.page_csrf(c, page), **data}, follow_redirects=False
    )


def configure(c: TestClient, port: int, **extra: str) -> Any:
    return post(
        c,
        "/admin/discord",
        "/admin/discord/smtp",
        {
            "host": "127.0.0.1",
            "port": str(port),
            "tls": "none",
            "username": "",
            "from_address": "wp@example.invalid",
            "admin_password": web.PASSWORD,
        }
        | extra,
    )


def dispatch(w: World, real: SimClock) -> Any:
    return Dispatcher(w.settings, real, w.clock).run_pass(w.engine)


def player(w: World, real: SimClock) -> int:
    code = auth.rotate_registration_code(w.engine, real)
    return auth.register(
        w.engine,
        real,
        email=PLAYER,
        display_name="Pat",
        password=web.PASSWORD,
        code=code,
        ip="10.0.0.5",
    )


def test_hidden_when_smtp_is_off(site: tuple[TestClient, World, SimClock]) -> None:
    c, w, real = site
    anon = web.client(create_app(w.settings, auth_clock=real, domain_clock=w.clock))
    with anon:
        assert 'data-testid="forgot"' not in anon.get("/login").text
        assert anon.get("/reset").status_code == 404
        assert anon.get("/reset/abc").status_code == 404
    with w.engine.connect() as conn:
        assert (
            conn.execute(select(OutboxMessage).where(OutboxMessage.channel == "email")).first()
            is None
        )
    assert post(c, "/admin/discord", "/admin/discord/smtp/test", {}).status_code == 400


def test_settings_and_test_email(site: tuple[TestClient, World, SimClock], smtp: FakeSmtp) -> None:
    c, w, real = site
    bad = configure(c, smtp.port, from_address="nope")
    assert bad.status_code == 400 and "From address" in bad.text
    assert configure(c, smtp.port, admin_password="x").status_code == 403
    assert (
        configure(c, smtp.port, password="smtp-pass").headers["location"]
        == "/admin/discord?ok=smtp"
    )
    page = c.get("/admin/discord").text
    assert 'data-testid="smtp-status">on' in page and "smtp-pass" not in page
    assert post(c, "/admin/discord", "/admin/discord/smtp/test", {}).status_code == 303
    assert dispatch(w, real).sent == 1
    (msg,) = smtp.inbox.messages
    assert msg["To"] == "admin@example.invalid" and "test email" in msg["Subject"]
    cleared = post(
        c, "/admin/discord", "/admin/discord/smtp", {"clear": "on", "admin_password": web.PASSWORD}
    )
    assert (
        cleared.status_code == 303
        and 'data-testid="smtp-status">off' in c.get("/admin/discord").text
    )


def test_password_reset_by_email(site: tuple[TestClient, World, SimClock], smtp: FakeSmtp) -> None:
    c, w, real = site
    configure(c, smtp.port)
    player(w, real)
    anon = web.client(create_app(w.settings, auth_clock=real, domain_clock=w.clock))
    with anon:
        web.log_in(anon, PLAYER)  # an existing session that the reset must end
        other = web.client(create_app(w.settings, auth_clock=real, domain_clock=w.clock))
        with other:
            assert 'data-testid="forgot"' in other.get("/login").text
            token = web.form_csrf(other, "/reset")
            r = other.post("/api/auth/reset", data={"csrf_token": token, "email": PLAYER.upper()})
            assert 'data-testid="reset-sent"' in r.text
            r2 = other.post(
                "/api/auth/reset", data={"csrf_token": token, "email": "nobody@example.invalid"}
            )
            assert (
                r2.text.replace("nobody@example.invalid", "").count("reset-sent") == 1
            )  # same answer
            with w.engine.connect() as conn:
                rows = (
                    conn.execute(
                        select(OutboxMessage.payload).where(OutboxMessage.channel == "email")
                    )
                    .scalars()
                    .all()
                )
            assert len(rows) == 1  # only the real account got one
            assert PLAYER not in str(rows[0]) and "sealed" in rows[0]
            assert dispatch(w, real).sent == 1
            (body,) = smtp.inbox.bodies()
            link = re.search(r"https://wp\.example/reset/(\S+)", body)
            assert link is not None
            secret = link.group(1)
            with w.engine.connect() as conn:
                sent = conn.execute(
                    select(OutboxMessage.payload).where(OutboxMessage.channel == "email")
                ).scalar_one()
                stored = conn.execute(select(PasswordReset.token_hash)).scalar_one()
            assert "sealed" not in sent and secret not in stored  # token dropped; hash only
            assert other.get(f"/reset/{secret}").status_code == 200
            assert other.get("/reset/not-a-token").status_code == 410
            form = web.form_csrf(other, f"/reset/{secret}")
            weak = other.post(
                "/api/auth/reset/complete",
                data={"csrf_token": form, "token": secret, "password": "short", "confirm": "short"},
            )
            assert weak.status_code == 400 and "at least 10" in weak.text
            done = other.post(
                "/api/auth/reset/complete",
                data={
                    "csrf_token": form,
                    "token": secret,
                    "password": "brand new secret",
                    "confirm": "brand new secret",
                },
                follow_redirects=False,
            )
            assert done.headers["location"] == "/login?reset=1"
            again = other.post(
                "/api/auth/reset/complete",
                data={
                    "csrf_token": form,
                    "token": secret,
                    "password": "another secret 1",
                    "confirm": "another secret 1",
                },
            )
            assert again.status_code == 400 and "expired or was already used" in again.text
        with w.engine.connect() as conn:
            assert conn.execute(select(Session).where(Session.user_id != 1)).first() is None
        assert anon.get("/bets/mine", follow_redirects=False).status_code == 303  # logged out
    fresh = web.client(create_app(w.settings, auth_clock=real, domain_clock=w.clock))
    with fresh:
        web.log_in(fresh, PLAYER, "brand new secret")


def test_links_expire_and_requests_are_rate_limited(
    site: tuple[TestClient, World, SimClock], smtp: FakeSmtp
) -> None:
    c, w, real = site
    configure(c, smtp.port)
    player(w, real)
    password_reset.request(w.engine, real, w.settings, address=PLAYER, ip="1.1.1.1")
    dispatch(w, real)
    secret = re.search(r"/reset/(\S+)", smtp.inbox.bodies()[0]).group(1)  # type: ignore[union-attr]
    real.set(real.now() + timedelta(hours=1, minutes=1))
    assert password_reset.check(w.engine, real, secret) is False
    for _ in range(2):
        password_reset.request(w.engine, real, w.settings, address=PLAYER, ip="2.2.2.2")
    password_reset.request(w.engine, real, w.settings, address=PLAYER, ip="3.3.3.3")
    with pytest.raises(password_reset.ResetError, match="Too many"):  # 3 per address per hour
        password_reset.request(w.engine, real, w.settings, address=PLAYER, ip="4.4.4.4")
    for n in range(5):
        password_reset.request(
            w.engine, real, w.settings, address=f"x{n}@example.invalid", ip="9.9.9.9"
        )
    with pytest.raises(password_reset.ResetError):  # 5 per IP per hour
        password_reset.request(
            w.engine, real, w.settings, address="y@example.invalid", ip="9.9.9.9"
        )


def test_smtp_failures_retry_or_give_up(
    site: tuple[TestClient, World, SimClock], smtp: FakeSmtp
) -> None:
    c, w, real = site
    configure(c, smtp.port, username="user", password="wrong")
    smtp.inbox.reject_auth = True
    post(c, "/admin/discord", "/admin/discord/smtp/test", {})
    result = dispatch(w, real)
    assert result.dead == 1  # bad credentials: retrying won't help
    configure(c, 1)  # nothing listens on port 1: a network error, retried later
    post(c, "/admin/discord", "/admin/discord/smtp/test", {})
    assert dispatch(w, real).retried == 1


def test_emails_are_paced_one_per_pass(
    site: tuple[TestClient, World, SimClock], smtp: FakeSmtp
) -> None:
    c, w, real = site
    configure(c, smtp.port)
    for _ in range(3):
        post(c, "/admin/discord", "/admin/discord/smtp/test", {})
    first = dispatch(w, real)
    assert first.sent == 1 and first.waiting >= 1 and first.more  # the fast loop comes back
    assert [dispatch(w, real).sent for _ in range(3)] == [1, 1, 0]
    assert len(smtp.inbox.messages) == 3
