"""Dev-only routes (`APP_ENV=dev`): the simulation clock and simulator controls.

Mounted only in dev and reachable only on the loopback port. Admin only; every POST
carries the CSRF token like the rest of the app.
"""

from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlencode

from fastapi import APIRouter, Depends, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import Engine, func, select

from app.core.config import Settings
from app.models import Observation, SyncRun
from app.providers.simulated import PRESETS
from app.services import sim
from app.services.observations import canonical_weigh_ins, latest_complete_through
from app.web import format as fmt
from app.web.security import require_admin, require_session

TEMPLATES = Jinja2Templates(directory=Path(__file__).parent / "templates")


async def _form(request: Request) -> dict[str, str]:
    body = (await request.body()).decode()
    return {key: values[-1] for key, values in parse_qs(body).items()}


def build_router(settings: Settings, engine: Engine) -> APIRouter:
    router = APIRouter(
        prefix="/dev", include_in_schema=False, dependencies=[Depends(require_admin)]
    )

    def back(error: str | None = None) -> RedirectResponse:
        url = "/dev/clock" + (f"?{urlencode({'error': error})}" if error else "")
        return RedirectResponse(url, status_code=303)

    @router.get("/clock")
    def clock_page(request: Request, error: str | None = None) -> Response:
        state = sim.load_state(engine, settings)
        with engine.connect() as conn:
            counts = dict(
                conn.execute(
                    select(Observation.metric, func.count()).group_by(Observation.metric)
                ).all()
            )
            last_sync = conn.execute(
                select(SyncRun.status, SyncRun.started_at, SyncRun.rows_new)
                .order_by(SyncRun.id.desc())
                .limit(1)
            ).one_or_none()
            canonical = canonical_weigh_ins(
                conn, state.sim_now.date() - timedelta(days=14), state.sim_now.date()
            )
            complete = latest_complete_through(conn)
        return TEMPLATES.TemplateResponse(
            request,
            "dev_clock.html",
            {
                "state": state,
                "local_now": state.sim_now.astimezone(settings.tz),
                "unit": settings.wp_unit,
                "presets": sorted(PRESETS),
                "counts": sorted(counts.items()),
                "last_sync": last_sync,
                "canonical": [(c.local_date, fmt.tenths(c.value), c.source) for c in canonical],
                "complete": sorted(complete.items()),
                "error": error,
                "csrf": require_admin(require_session(request)).csrf_token,
            },
        )

    @router.post("/clock/advance")
    async def advance(request: Request) -> Response:
        form = await _form(request)
        try:
            delta = timedelta(days=int(form.get("days") or 0), hours=int(form.get("hours") or 0))
            await run_in_threadpool(sim.advance, engine, settings, delta)
        except ValueError as exc:
            return back(str(exc))
        return back()

    @router.post("/clock/set")
    async def set_time(request: Request) -> Response:
        form = await _form(request)
        try:
            local = datetime.fromisoformat(form["to"]).replace(tzinfo=settings.tz)
            sim.set_now(engine, settings, local)
        except (KeyError, ValueError) as exc:
            return back(str(exc))
        return back()

    @router.post("/clock/reseed")
    async def reseed(request: Request) -> Response:
        form = await _form(request)
        try:
            sim.reseed(engine, settings, preset=form.get("preset", ""), seed=int(form["seed"]))
        except (KeyError, ValueError) as exc:
            return back(str(exc))
        return back()

    return router
