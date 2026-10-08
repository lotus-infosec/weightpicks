"""Integrations (Discord, Workers AI, SMTP), feature flags and the audit log."""

from fastapi import APIRouter, Request, Response
from fastapi.concurrency import run_in_threadpool

from app.domain import setup as setup_steps
from app.domain.urls import InvalidPublicUrl, parse_public_url
from app.notify import email as email_settings
from app.services import (
    admin,
    admin_ai,
    admin_views,
    instance,
    timezone,
)
from app.services import secrets as secret_store
from app.services.admin import AdminError
from app.web.admin.common import REAUTH_FAILED_MESSAGE, actor, back, reauth, render
from app.web.security import Admin, read_form
from app.web.setup import WEBHOOK_LABELS


def register(router: APIRouter) -> None:
    # ---- discord and flags -----------------------------------------------------------------------

    def discord_page(
        request: Request,
        error: str | None = None,
        status: int = 200,
        *,
        tz_preview: timezone.Impact | None = None,
        tz_error: str | None = None,
        url_error: str | None = None,
        url_value: str | None = None,
    ) -> Response:
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
        public_url, public_url_source = instance.public_url(config, state.settings)
        return render(
            request,
            "admin/discord.html",
            {
                "public_url": public_url,
                "public_url_source": public_url_source,
                "url_value": public_url if url_value is None else url_value,
                "url_error": url_error,
                "timezone": config.timezone if config else state.settings.wp_timezone,
                "zones": setup_steps.COMMON_ZONES,
                "tz_preview": tz_preview,
                "tz_error": tz_error,
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

    # ---- instance: public URL (#17) and time zone (#30) ---------------------------------------

    @router.post("/discord/public-url")
    async def discord_public_url(request: Request, session: Admin) -> Response:
        form = await read_form(request)
        state = request.app.state
        text = form.get("public_url", "")
        if not await reauth(request, session, form, "settings.public_url"):
            return discord_page(
                request, url_error=REAUTH_FAILED_MESSAGE, url_value=text, status=403
            )
        url: str | None = None
        if form.get("clear") != "on":
            try:
                url = parse_public_url(text, dev=state.settings.is_dev)
            except InvalidPublicUrl as exc:
                return discord_page(request, url_error=str(exc), url_value=text, status=400)
        await run_in_threadpool(
            admin.set_public_url, state.engine, state.auth_clock, actor(request, session), url
        )
        return back("/admin/discord", "public_url")

    @router.post("/discord/timezone/preview")
    async def discord_timezone_preview(request: Request, session: Admin) -> Response:
        form = await read_form(request)
        state = request.app.state
        try:
            with state.engine.connect() as conn:
                impact = timezone.preview(conn, form.get("timezone", ""), state.domain_clock.now())
        except timezone.TimezoneError as exc:
            return discord_page(request, tz_error=exc.message, status=400)
        if impact.zone == impact.current:
            return discord_page(request, tz_error=f"The time zone is already {impact.zone}.")
        return discord_page(request, tz_preview=impact)

    @router.post("/discord/timezone")
    async def discord_timezone(request: Request, session: Admin) -> Response:
        form = await read_form(request)
        state = request.app.state
        zone = form.get("timezone", "")

        def again(message: str, status: int) -> Response:
            try:
                with state.engine.connect() as conn:
                    impact = timezone.preview(conn, zone, state.domain_clock.now())
            except timezone.TimezoneError as exc:
                return discord_page(request, tz_error=exc.message, status=400)
            return discord_page(request, tz_preview=impact, tz_error=message, status=status)

        if not await reauth(request, session, form, "settings.timezone"):
            return again(REAUTH_FAILED_MESSAGE, 403)
        if form.get("confirm") != "on":
            return again("Tick the box to confirm the refunds.", 400)
        try:
            await run_in_threadpool(
                timezone.change, state.engine, state.domain_clock, actor(request, session), zone
            )
        except timezone.TimezoneError as exc:
            return discord_page(request, tz_error=exc.message, status=400)
        return back("/admin/discord", "timezone")

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
