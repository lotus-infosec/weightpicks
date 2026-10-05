"""Props and futures, the AI review queue, AI runs and notes (D-041, D-042)."""

from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse

from app.services import (
    admin,
    admin_ai,
    admin_views,
    ai_props,
    instance,
)
from app.services import props as props_service
from app.services import stats as stats_service
from app.services.admin import AdminError
from app.web.admin.common import REAUTH_FAILED_MESSAGE, actor, back, reauth, render
from app.web.security import Admin, read_form


def register(router: APIRouter) -> None:
    # ---- props and futures (D-041) ---------------------------------------------------------

    def props_page(
        request: Request,
        template: str,
        form: dict[str, str] | None = None,
        preview: Any = None,
        error: str | None = None,
        status: int = 200,
    ) -> Response:
        state = request.app.state
        template = template if template in props_service.PROP_TEMPLATES else "milestone_by"
        with state.engine.connect() as conn:
            data = admin_views.props(conn, state.domain_clock.now(), state.settings.tz)
        config, today = data.config, data.today
        unit = config.unit if config else state.settings.wp_unit
        latest = data.latest_x10 / 10 if data.latest_x10 else None
        metrics = [
            (m, stats_service.METRIC_LABELS[m]) for m in (config.enabled_metrics if config else ())
        ]
        return render(
            request,
            "admin/props.html",
            {
                "enabled": bool(config and config.flags.get("props_futures")),
                "templates": list(props_service.LABELS.items()),
                "labels": props_service.LABELS,
                "short": {
                    "milestone_by": "Milestone",
                    "streak_reaches": "Streak",
                    "beat_last_week": "Week vs week",
                    "future_total_change": "Future",
                },
                "template": template,
                "form": form or {},
                "preview": preview,
                "error": error,
                "unit": unit,
                "trend_text": f"latest weigh-in {latest:.1f} {unit}"
                if latest
                else "no recent weigh-ins",
                "suggest_threshold": f"{(latest or 0) - 1:.1f}" if latest else "",
                "suggest_deadline": (today + timedelta(days=7)).isoformat(),
                "min_day": (today + timedelta(days=2)).isoformat(),
                "max_day": (today + timedelta(days=28)).isoformat(),
                "suggest_future": (today + timedelta(days=30)).isoformat(),
                "min_future": (today + timedelta(days=2)).isoformat(),
                "max_future": (today + timedelta(days=120)).isoformat(),
                "metrics": metrics,
                "open_props": data.open_props,
                "ai_queue": data.ai_queue,
                "ai_on": bool(config and config.flags.get("ai_props")),
                "ai_mode": config.ai_mode if config else "review",
                "ai_configured": data.ai_configured,
                "ai_last_run": data.ai_last_run,
                "active": "props",
            },
            status,
        )

    @router.get("/props")
    def props(request: Request, session: Admin, template: str = "milestone_by") -> Response:
        return props_page(request, template)

    @router.post("/props/preview")
    async def props_preview(request: Request, session: Admin) -> Response:
        form = await read_form(request)
        state = request.app.state
        template = form.get("template", "")
        try:
            with state.engine.connect() as conn:
                config = instance.read(conn)
                if config is None:
                    raise props_service.PropError("Finish setup first.")
                today = state.domain_clock.now().astimezone(config.tz).date()
                result = props_service.preview(conn, config, today, template, dict(form))
        except props_service.PropError as exc:
            return props_page(request, template, dict(form), error=exc.message, status=400)
        return props_page(request, template, dict(form), preview=result)

    @router.post("/props/create")
    async def props_create(request: Request, session: Admin) -> Response:
        form = await read_form(request)
        state = request.app.state
        template = form.get("template", "")
        if not await reauth(request, session, form, "market.create_prop"):
            return props_page(
                request, template, dict(form), error=REAUTH_FAILED_MESSAGE, status=403
            )
        try:
            await run_in_threadpool(
                props_service.create,
                state.engine,
                state.domain_clock,
                actor(request, session),
                template,
                dict(form),
            )
        except props_service.PropError as exc:
            return props_page(request, template, dict(form), error=exc.message, status=400)
        return RedirectResponse(f"/admin/props?template={template}&ok=prop", status_code=303)

    # ---- AI review queue, mode, runs and notes (D-042) ------------------------------------

    @router.post("/props/ai/{proposal_id}/approve")
    async def ai_approve(request: Request, session: Admin, proposal_id: int) -> Response:
        state = request.app.state
        try:
            await run_in_threadpool(
                ai_props.approve,
                state.engine,
                state.domain_clock,
                actor(request, session),
                proposal_id,
            )
        except ai_props.ApprovalError as exc:
            return props_page(request, "milestone_by", error=exc.message, status=409)
        return back("/admin/props", "approved")

    @router.post("/props/ai/{proposal_id}/reject")
    async def ai_reject(request: Request, session: Admin, proposal_id: int) -> Response:
        state = request.app.state
        try:
            await run_in_threadpool(
                ai_props.reject,
                state.engine,
                state.domain_clock,
                actor(request, session),
                proposal_id,
            )
        except ai_props.ApprovalError as exc:
            return props_page(request, "milestone_by", error=exc.message, status=409)
        return back("/admin/props", "rejected")

    @router.post("/props/ai/mode")
    async def ai_mode(request: Request, session: Admin) -> Response:
        form = await read_form(request)
        state = request.app.state
        if not await reauth(request, session, form, "settings.ai_mode"):
            return props_page(request, "milestone_by", error=REAUTH_FAILED_MESSAGE, status=403)
        try:
            await run_in_threadpool(
                admin_ai.set_ai_mode,
                state.engine,
                state.auth_clock,
                actor(request, session),
                form.get("ai_mode", ""),
            )
        except AdminError as exc:
            return props_page(request, "milestone_by", error=exc.message, status=400)
        return back("/admin/props", "ai_mode")

    @router.post("/props/ai/run")
    async def ai_run_now(request: Request, session: Admin) -> Response:
        state = request.app.state
        await run_in_threadpool(
            admin.request_command,
            state.engine,
            state.auth_clock,
            actor(request, session),
            "ai_props_now",
        )
        return back("/admin/props", "ai_run")

    @router.get("/ai")
    def ai_runs(request: Request, session: Admin) -> Response:
        state = request.app.state
        with state.engine.connect() as conn:
            view = admin_ai.runs_view(
                conn, state.auth_clock.now(), state.settings.ai_daily_neuron_cap
            )
            configured = admin_ai.workers_ai_configured(conn)
        return render(
            request, "admin/ai.html", {"v": view, "configured": configured, "active": "ai"}
        )

    def notes_page(request: Request, error: str | None = None, status: int = 200) -> Response:
        state = request.app.state
        with state.engine.connect() as conn:
            config = instance.read(conn)
            tz = config.tz if config else state.settings.tz
            today = state.domain_clock.now().astimezone(tz).date()
            rows = admin_ai.notes(conn, today)
        return render(
            request,
            "admin/notes.html",
            {
                "notes": rows,
                "today": today.isoformat(),
                "max_to": (today + timedelta(days=admin_ai.NOTE_MAX_DAYS)).isoformat(),
                "error": error,
                "active": "notes",
            },
            status,
        )

    @router.get("/notes")
    def notes(request: Request, session: Admin) -> Response:
        return notes_page(request)

    @router.post("/notes")
    async def notes_add(request: Request, session: Admin) -> Response:
        form = await read_form(request)
        state = request.app.state
        try:
            start = date.fromisoformat(form.get("active_from", ""))
            end = date.fromisoformat(form.get("active_to", ""))
        except ValueError:
            return notes_page(request, "Pick a start and end date.", 400)
        try:
            await run_in_threadpool(
                admin_ai.add_note,
                state.engine,
                state.auth_clock,
                actor(request, session),
                form.get("text", ""),
                start,
                end,
            )
        except AdminError as exc:
            return notes_page(request, exc.message, 400)
        return back("/admin/notes", "note")

    @router.post("/notes/{note_id}/end")
    async def notes_end(request: Request, session: Admin, note_id: int) -> Response:
        state = request.app.state
        with state.engine.connect() as conn:
            config = instance.read(conn)
        tz = config.tz if config else state.settings.tz
        today = state.domain_clock.now().astimezone(tz).date()
        try:
            await run_in_threadpool(
                admin_ai.end_note,
                state.engine,
                state.auth_clock,
                actor(request, session),
                note_id,
                today,
            )
        except AdminError as exc:
            return notes_page(request, exc.message, 404)
        return back("/admin/notes", "note_ended")
