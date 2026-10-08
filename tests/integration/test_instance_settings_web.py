"""Admin → Settings → Instance: the public URL (issue #17) and the time zone (#30)."""

from collections.abc import Iterator
from datetime import date
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select

from app.core.config import Settings
from app.models import AuditEntry, Market
from app.services import instance
from app.web.main import LiveInstance, create_app
from tests.integration import web
from tests.integration.test_bets_settlement import bet, player, weight_market
from tests.integration.world import World, local

PAGE = "/admin/discord"


@pytest.fixture
def admin(world: World) -> Iterator[tuple[TestClient, World]]:
    world.settings = world.settings.model_copy(update={"app_secret_key": SecretStr("k" * 40)})
    c = web.client(create_app(world.settings, domain_clock=world.clock))
    with c:
        web.as_admin(c, world.engine)
        yield c, world


def post(c: TestClient, action: str, **data: str) -> Any:
    return c.post(
        action, data={"csrf_token": web.page_csrf(c, PAGE), **data}, follow_redirects=False
    )


def stored_url(w: World) -> str | None:
    with w.engine.connect() as conn:
        config = instance.read(conn)
    assert config is not None
    return config.public_url


def test_public_url_save_validate_clear(admin: tuple[TestClient, World]) -> None:
    c, w = admin
    page = c.get(PAGE).text
    assert 'data-testid="instance-settings"' in page and "from .env" in page
    r = post(c, "/admin/discord/public-url", public_url="https://x.example", admin_password="no")
    assert r.status_code == 403 and stored_url(w) is None
    r = post(
        c,
        "/admin/discord/public-url",
        public_url="https://x.example/app",
        admin_password=web.PASSWORD,
    )
    assert r.status_code == 400 and "no path" in r.text and stored_url(w) is None
    r = post(
        c, "/admin/discord/public-url", public_url="https://X.example/", admin_password=web.PASSWORD
    )
    assert r.status_code == 303 and stored_url(w) == "https://x.example"
    assert "set here" in c.get(PAGE).text
    r = post(c, "/admin/discord/public-url", clear="on", admin_password=web.PASSWORD)
    assert r.status_code == 303 and stored_url(w) is None
    with w.engine.connect() as conn:
        changes = conn.execute(
            select(AuditEntry.before, AuditEntry.after).where(
                AuditEntry.action == "settings.public_url"
            )
        ).all()
    assert [(b["public_url"], a["public_url"]) for b, a in changes] == [
        (None, "https://x.example"),
        ("https://x.example", None),
    ]


def test_loopback_warning_only_in_production() -> None:
    key = SecretStr("k" * 40)
    prod = Settings(app_env="production", app_secret_key=key, wp_base_url="http://127.0.0.1:8000")
    assert LiveInstance(prod).public_url_warning
    assert not LiveInstance(prod.model_copy(update={"app_env": "dev"})).public_url_warning
    public = prod.model_copy(update={"wp_base_url": "https://picks.example.com"})
    assert not LiveInstance(public).public_url_warning
    live = LiveInstance(prod)
    live.config = instance.InstanceConfig(
        timezone="UTC",
        unit="lb",
        schedule=instance.schedule_from_json({}),
        enabled_metrics=(),
        state="active",
        public_url="https://picks.example.com",
    )
    assert not live.public_url_warning  # the admin's setting wins


def test_timezone_preview_confirm_and_change(admin: tuple[TestClient, World]) -> None:
    c, w = admin
    w.clock.set(local(2026, 10, 5, 12, 30))
    p = player(w, 1)
    bet(w, p, weight_market(w, -5, day=date(2026, 10, 7)), "over")
    r = post(c, "/admin/discord/timezone/preview", timezone="Mars/Olympus")
    assert r.status_code == 400 and "time zone such as" in r.text
    r = post(c, "/admin/discord/timezone/preview", timezone="America/Chicago")
    assert r.status_code == 200 and 'data-testid="timezone-warning"' in r.text
    assert "1 market(s): 1 bet(s)" in r.text and "pushed and refunded" in r.text
    no_box = post(
        c, "/admin/discord/timezone", timezone="America/Chicago", admin_password=web.PASSWORD
    )
    assert no_box.status_code == 400 and "Tick the box" in no_box.text
    bad_pw = post(
        c, "/admin/discord/timezone", timezone="America/Chicago", confirm="on", admin_password="x"
    )
    assert bad_pw.status_code == 403
    with w.engine.connect() as conn:
        assert instance.read(conn).timezone == "America/New_York"  # type: ignore[union-attr]
    r = post(
        c,
        "/admin/discord/timezone",
        timezone="America/Chicago",
        confirm="on",
        admin_password=web.PASSWORD,
    )
    assert r.status_code == 303 and r.headers["location"].endswith("ok=timezone")
    page = c.get(r.headers["location"]).text
    assert "Time zone changed" in page and ">America/Chicago<" in page
    with w.engine.connect() as conn:
        assert set(
            conn.execute(
                select(Market.status).where(Market.dedupe_key.not_like("%@America/Chicago"))
            ).scalars()
        ) == {"voided"}
    same = post(c, "/admin/discord/timezone/preview", timezone="America/Chicago")
    assert "already America/Chicago" in same.text
