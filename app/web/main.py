"""FastAPI app factory: placeholder page, health check, robots and security headers."""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates

from app.core.config import Settings
from app.core.db import make_engine
from app.core.logging import configure_logging
from app.core.migrations import is_at_head

TEMPLATES_DIR = Path(__file__).parent / "templates"
SECURITY_HEADERS = {
    "X-Robots-Tag": "noindex, nofollow",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Content-Security-Policy": (
        "default-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    ),
}
ROBOTS_TXT = "User-agent: *\nDisallow: /\n"

log = structlog.get_logger()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    configure_logging(settings.log_level, settings.log_format)
    engine = make_engine(settings.db_url)
    templates = Jinja2Templates(directory=TEMPLATES_DIR)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        log.info("web_started", app_env=settings.app_env)
        yield
        engine.dispose()

    app = FastAPI(
        title="WeightPicks", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
    )

    @app.middleware("http")
    async def security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        response.headers.update(SECURITY_HEADERS)
        return response

    @app.get("/", include_in_schema=False)
    def index(request: Request) -> Response:
        return templates.TemplateResponse(request, "index.html")

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> Response:
        try:
            schema_ok = is_at_head(engine)
            templates.get_template("index.html").render()
        except Exception:
            log.exception("healthz_failed")
            return JSONResponse({"status": "error"}, status_code=503)
        if not schema_ok:
            return JSONResponse({"status": "migrating"}, status_code=503)
        return JSONResponse({"status": "ok"})

    if settings.is_dev and settings.data_provider == "simulated":
        from app.web.dev import build_router

        app.include_router(build_router(settings, engine))

    @app.get("/robots.txt", include_in_schema=False)
    def robots() -> PlainTextResponse:
        return PlainTextResponse(ROBOTS_TXT)

    return app
