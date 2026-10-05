"""Integrations (Discord, Workers AI, SMTP), feature flags and the audit log."""

from fastapi import APIRouter, Request, Response
from fastapi.concurrency import run_in_threadpool

from app.notify import email as email_settings
from app.services import (
    admin,
    admin_ai,
    admin_views,
    instance,
)
from app.services import secrets as secret_store
from app.services.admin import AdminError
from app.web.admin.common import REAUTH_FAILED_MESSAGE, actor, back, reauth, render
from app.web.security import Admin, read_form
from app.web.setup import WEBHOOK_LABELS


def register(router: APIRouter) -> None:
    # ---- discord and flags -----------------------------------------------------------------------

    def discord_page(request: Request, error: str | None = None, status: int = 200) -> Response:
        state = request.app.state
        with state.engine.connect() as conn:
            configured = {
                n.split(".", 1)[1] for n in secret_store.names(conn) if n.startswith("webhook.")
            }
            config = instance.read(conn)
            recent = admin_views.recent_outbox(conn)
            ai_configured = admin_ai.workers_ai_configured(conn)
            smtp = email_settings.stored(conn)
            smtp_password = "smtp.password" in secret_store.names(conn)
        return render(
            request,
            "admin/discord.html",
            {
                "categories": list(WEBHOOK_LABELS.items()),
                "labels": WEBHOOK_LABELS,
                "configured": configured,
                "flags": config.flags if config else {},
                "recent": recent,
                "ai_configured": ai_configured,
                "smtp": smtp,
                "smtp_password": smtp_password,
                "can_store": bool(state.settings.app_secret_key.get_secret_value()),
                "error": error,
                "active": "discord",
            },
            status,
        )

    @router.get("/discord")
    def discord(request: Request, session: Admin) -> Response:
        return discord_page(request)

    @router.post("/discord/webhook")
    async def discord_webhook(request: Request, session: Admin) -> Response:
        form = await read_form(request)
        state = request.app.state
        if not await reauth(request, session, form, "discord.webhook"):
            return discord_page(request, REAUTH_FAILED_MESSAGE, 403)
        try:
            await run_in_threadpool(
                admin.set_webhook,
                state.engine,
                state.auth_clock,
                actor(request, session),
                state.settings.app_secret_key.get_secret_value(),
                form.get("category", ""),
                form.get("url", ""),
            )
        except AdminError as exc:
            return discord_page(request, exc.message, 400)
        return back("/admin/discord", "webhook")

    @router.post("/discord/workers-ai")
    async def discord_workers_ai(request: Request, session: Admin) -> Response:
        form = await read_form(request)
        state = request.app.state
        if not await reauth(request, session, form, "integrations.workers_ai"):
            return discord_page(request, REAUTH_FAILED_MESSAGE, 403)
        try:
            await run_in_threadpool(
                admin_ai.set_workers_ai,
                state.engine,
                state.auth_clock,
                actor(request, session),
                state.settings.app_secret_key.get_secret_value(),
                form.get("account_id", ""),
                form.get("token", ""),
                form.get("clear") == "on",
            )
        except AdminError as exc:
            return discord_page(request, exc.message, 400)
        return back("/admin/discord", "workers_ai")

    @router.post("/discord/smtp")
    async def discord_smtp(request: Request, session: Admin) -> Response:
        form = dict(await read_form(request))
        state = request.app.state
        if not await reauth(request, session, form, "integrations.smtp"):
            return discord_page(request, REAUTH_FAILED_MESSAGE, 403)
        try:
            errors = await run_in_threadpool(
                admin_ai.set_smtp,
                state.engine,
                state.auth_clock,
                actor(request, session),
                state.settings.app_secret_key.get_secret_value(),
                form,
                form.get("clear") == "on",
            )
        except AdminError as exc:
            return discord_page(request, exc.message, 400)
        if errors:
            return discord_page(request, " ".join(errors.values()), 400)
        return back("/admin/discord", "smtp")

    @router.post("/discord/smtp/test")
    async def discord_smtp_test(request: Request, session: Admin) -> Response:
        state = request.app.state
        queued = await run_in_threadpool(
            admin_ai.send_smtp_test, state.engine, state.auth_clock, actor(request, session)
        )
        if not queued:
            return discord_page(request, "Set up email first.", 400)
        return back("/admin/discord", "smtp_test")

    @router.post("/discord/test/{category}")
    async def discord_test(request: Request, session: Admin, category: str) -> Response:
        state = request.app.state
        try:
            await run_in_threadpool(
                admin.send_test, state.engine, state.domain_clock, actor(request, session), category
            )
        except AdminError as exc:
            return discord_page(request, exc.message, 404)
        return back("/admin/discord", "test")

    @router.post("/discord/flags")
    async def discord_flags(request: Request, session: Admin) -> Response:
        form = await read_form(request)
        state = request.app.state
        for flag in admin.ADMIN_FLAGS:
            await run_in_threadpool(
                admin.set_flag,
                state.engine,
                state.auth_clock,
                actor(request, session),
                flag,
                form.get(flag) == "on",
            )
        return back("/admin/discord", "flags")

    # ---- audit ----------------------------------------------------------------------------------

    @router.get("/audit")
    def audit(request: Request, session: Admin, page: int = 0) -> Response:
        page = max(page, 0)
        with request.app.state.engine.connect() as conn:
            rows = admin_views.audit_page(conn, page)
        return render(
            request,
            "admin/audit.html",
            {
                "rows": rows[: admin_views.PAGE],
                "page": page,
                "more": len(rows) > admin_views.PAGE,
                "active": "audit",
            },
        )
