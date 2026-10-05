"""Brand images: project defaults, overridden by an Appearance upload (STAGE15)."""

from pathlib import Path

from sqlalchemy import Engine

from app.core.config import Settings
from app.web.main import STATIC_DIR, create_app
from tests.integration import web


def test_defaults_upload_override_and_unknown(migrated_engine: Engine, settings: Settings) -> None:
    c = web.client(create_app(settings))
    with c:
        default = (STATIC_DIR / "brand" / "favicon-32.png").read_bytes()
        r = c.get("/brand/favicon.png")  # served before setup too (login page favicon)
        assert r.status_code == 200 and r.content == default
        assert r.headers["content-type"] == "image/png"
        assert r.headers["x-content-type-options"] == "nosniff"
        assert c.get("/brand/logo.png").status_code == 200
        assert c.get("/brand/..%2Fapp.db").status_code == 404  # only the named files
        assert c.get("/brand/secrets.txt").status_code == 404
        uploads = Path(settings.data_dir) / "uploads"
        uploads.mkdir()
        (uploads / "favicon.png").write_bytes(b"\x89PNG-instance")
        assert c.get("/brand/favicon.png").content == b"\x89PNG-instance"
        m = c.get("/manifest.webmanifest")
        assert m.status_code == 200 and m.json()["icons"][1]["src"] == "/brand/logo.png"
