from fastapi.testclient import TestClient
from sqlalchemy import Engine

from app.core.config import Settings
from app.web.main import create_app


def test_healthz_ok_with_noindex_and_security_headers(
    settings: Settings, migrated_engine: Engine
) -> None:
    with TestClient(create_app(settings)) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["X-Robots-Tag"] == "noindex, nofollow"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert "default-src 'self'" in response.headers["Content-Security-Policy"]


def test_healthz_unavailable_until_migrated(settings: Settings) -> None:
    with TestClient(create_app(settings)) as client:
        response = client.get("/healthz")
    assert response.status_code == 503
    assert response.json() == {"status": "migrating"}


def test_placeholder_page_and_robots(settings: Settings, migrated_engine: Engine) -> None:
    with TestClient(create_app(settings)) as client:
        page = client.get("/")
        robots = client.get("/robots.txt")
        docs = client.get("/docs")
    assert page.status_code == 200
    assert '<meta name="robots" content="noindex, nofollow">' in page.text
    assert page.headers["X-Robots-Tag"] == "noindex, nofollow"
    assert robots.text == "User-agent: *\nDisallow: /\n"
    assert docs.status_code == 404  # no public API docs
