"""FastAPI app factory: security headers, CSRF, sessions, pages and health checks."""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import structlog
from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import Engine

from app.core.clock import Clock, SystemClock
from app.core.config import Settings
from app.core.db import make_engine
from app.core.logging import configure_logging
from app.core.migrations import is_at_head
from app.web import format as fmt
from app.web.security import Forbidden, LoginRequired, csrf_protect

WEB_DIR = Path(__file__).parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"
LOCAL_CSS = STATIC_DIR / "build" / "app.css"  # scripts/build-css.sh (git-ignored)
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "object-src 'none'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
)
SECURITY_HEADERS = {
    "X-Robots-Tag": "noindex, nofollow",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Content-Security-Policy": CSP,
}
ROBOTS_TXT = "User-agent: *\nDisallow: /\n"

log = structlog.get_logger()


class LazyAppClock:
    """The domain clock (persisted SimClock in dev), resolved on first use so the app
    can start before migrations finish."""

    def __init__(self, settings: Settings, engine: Engine) -> None:
        self._settings, self._engine = settings, engine
        self._clock: Clock | None = None

    def now(self) -> datetime:
        if self._clock is None:
            from app.services.sim import app_clock

            self._clock = app_clock(self._settings, self._engine)
        return self._clock.now()


def build_templates(settings: Settings) -> Jinja2Templates:
    templates = Jinja2Templates(directory=TEMPLATES_DIR)
    env = templates.env
    env.filters["money"] = fmt.money
    env.filters["signed_money"] = fmt.signed_money
    env.filters["odds"] = fmt.odds
    env.filters["local_time"] = lambda ts: fmt.local_time(ts, settings.tz)
    env.filters["line"] = lambda x10, metric: fmt.line(x10, metric, settings.wp_unit)
    env.globals["unit"] = settings.wp_unit
    env.globals["csrf"] = ""  # pages without a session (errors) still render
    env.globals["session"] = None
    return templates


def create_app(
    settings: Settings | None = None,
    *,
    auth_clock: Clock | None = None,
    domain_clock: Clock | None = None,
) -> FastAPI:
    settings = settings or Settings()
    configure_logging(settings.log_level, settings.log_format)
    engine = make_engine(settings.db_url)
    templates = build_templates(settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        log.info("web_started", app_env=settings.app_env)
        yield
        engine.dispose()

    app = FastAPI(
        title="WeightPicks",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
        dependencies=[Depends(csrf_protect)],
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.templates = templates
    # Sessions and rate limits run on real time; markets and bets on the app clock.
    app.state.auth_clock = auth_clock or SystemClock()
    app.state.domain_clock = domain_clock or LazyAppClock(settings, engine)

    @app.middleware("http")
    async def security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        response.headers.update(SECURITY_HEADERS)
        if not request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(LoginRequired)
    async def login_required(request: Request, _exc: LoginRequired) -> Response:
        if request.method == "GET" and "HX-Request" not in request.headers:
            return RedirectResponse("/login", status_code=303)
        return JSONResponse(
            {"ok": False, "reason": "login_required"},
            status_code=401,
            headers={"HX-Redirect": "/login"},
        )

    @app.exception_handler(Forbidden)
    async def forbidden(request: Request, _exc: Forbidden) -> Response:
        if request.method == "GET" and "HX-Request" not in request.headers:
            return templates.TemplateResponse(
                request,
                "error.html",
                {"message": "You don't have access to this page."},
                status_code=403,
            )
        return JSONResponse({"ok": False, "reason": "forbidden"}, status_code=403)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> Response:
        try:
            schema_ok = is_at_head(engine)
            templates.get_template("login.html")
        except Exception:
            log.exception("healthz_failed")
            return JSONResponse({"status": "error"}, status_code=503)
        if not schema_ok:
            return JSONResponse({"status": "migrating"}, status_code=503)
        return JSONResponse({"status": "ok"})

    @app.get("/robots.txt", include_in_schema=False)
    def robots() -> PlainTextResponse:
        return PlainTextResponse(ROBOTS_TXT)

    @app.get("/static/app.css", include_in_schema=False)
    def stylesheet() -> Response:
        for candidate in (settings.wp_static_build_dir / "app.css", LOCAL_CSS):
            if candidate.is_file():
                return FileResponse(candidate, media_type="text/css")
        return Response(
            "/* stylesheet not built: run scripts/build-css.sh */\n", media_type="text/css"
        )

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    from app.web.pages import build_router

    app.include_router(build_router())
    if settings.is_dev and settings.data_provider == "simulated":
        from app.web.dev import build_router as build_dev_router

        app.include_router(build_dev_router(settings, engine))
    return app
