"""System health, backups, restore and reset; Appearance."""

from pathlib import Path

from fastapi import APIRouter, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from app.domain import setup as setup_steps
from app.services import (
    admin_ai,
    admin_views,
    appearance,
    backups,
    instance,
    maintenance,
)
from app.services import audit as audit_log
from app.services import secrets as secret_store
from app.web.admin.common import REAUTH_FAILED_MESSAGE, actor, back, reauth, render
from app.web.security import Admin, read_form


def register(router: APIRouter) -> None:
    # ---- system: health, backups, restore, reset --------------------

    def system_page(request: Request, error: str | None = None, status: int = 200) -> Response:
        state = request.app.state
        settings = state.settings
        now = state.auth_clock.now()
        with state.engine.connect() as conn:
            health = admin_views.system_health(conn, now, settings.ai_daily_neuron_cap)
            config = instance.read(conn)
            key = secret_store.key_status(conn, settings.app_secret_key.get_secret_value())
        from app.core.migrations import current_revision, head_revision

        return render(
            request,
            "admin/system.html",
            {
                "h": health,
                "backups_on": bool(config and config.flags.get("backup_ui")),
                "backups": backups.listing(settings),
                "staged": maintenance.pending_action(settings),
                "version": backups.app_version(),
                "revision": current_revision(state.engine),
                "head": head_revision(),
                "key_status": key,
                "app_name": config.app_name if config else "WeightPicks",
                "error": error,
                "active": "system",
            },
            status,
        )

    @router.get("/system")
    def system(request: Request, session: Admin) -> Response:
        return system_page(request)

    @router.post("/system/backup")
    async def system_backup(request: Request, session: Admin) -> Response:
        state = request.app.state
        path = await run_in_threadpool(
            lambda: backups.create(state.engine, state.settings, state.auth_clock, label="admin")
        )
        await run_in_threadpool(
            lambda: audit_log.record_alone(
                state.engine,
                state.auth_clock,
                actor(request, session),
                action="backup.create",
                after={"file": path.name},
            )
        )
        return back("/admin/system", "backup")

    @router.post("/system/download")
    async def system_download(request: Request, session: Admin) -> Response:
        form = await read_form(request)
        state = request.app.state
        if not await reauth(request, session, form, "backup.download"):
            return system_page(request, REAUTH_FAILED_MESSAGE, 403)
        path = backups.resolve(state.settings, form.get("name", ""))
        if path is None:
            return system_page(request, "No such backup.", 404)
        await run_in_threadpool(
            lambda: audit_log.record_alone(
                state.engine,
                state.auth_clock,
                actor(request, session),
                action="backup.download",
                after={"file": path.name},
            )
        )
        return FileResponse(path, media_type="application/gzip", filename=path.name)

    @router.post("/system/restore")
    async def system_restore(request: Request, session: Admin) -> Response:
        state = request.app.state
        form = await request.form(max_files=1, max_fields=10)
        fields = {k: v for k, v in form.items() if isinstance(v, str)}
        if not await reauth(request, session, fields, "maintenance.restore"):
            return system_page(request, REAUTH_FAILED_MESSAGE, 403)
        upload = form.get("file")
        source: Path | None = None
        if upload is not None and not isinstance(upload, str) and upload.filename:
            folder = backups.backups_dir(state.settings)
            folder.mkdir(parents=True, exist_ok=True)
            source = folder / f"wp-uploaded-{state.auth_clock.now():%Y%m%d-%H%M%S}-manual.tar.gz"
            with source.open("wb") as out:  # size already capped by BodyLimit
                while chunk := await upload.read(1024 * 1024):
                    out.write(chunk)
        elif fields.get("name"):
            source = backups.resolve(state.settings, fields["name"])
        if source is None:
            return system_page(request, "Choose a backup to restore.", 400)
        try:
            await run_in_threadpool(
                maintenance.stage_restore,
                state.engine,
                state.settings,
                state.auth_clock,
                actor(request, session),
                source,
            )
        except maintenance.MaintenanceError as exc:
            return system_page(request, exc.message, 400)
        return render_restart(request, "restore")

    @router.post("/system/reset")
    async def system_reset(request: Request, session: Admin) -> Response:
        form = await read_form(request)
        state = request.app.state
        if not await reauth(request, session, form, "maintenance.reset"):
            return system_page(request, REAUTH_FAILED_MESSAGE, 403)
        try:
            await run_in_threadpool(
                lambda: maintenance.stage_reset(
                    state.engine,
                    state.settings,
                    state.auth_clock,
                    actor(request, session),
                    confirm=form.get("confirm", ""),
                    wipe_garmin=form.get("wipe_garmin") == "on",
                )
            )
        except maintenance.MaintenanceError as exc:
            return system_page(request, exc.message, 400)
        return render_restart(request, "reset")

    def render_restart(request: Request, kind: str) -> Response:
        page = render(request, "admin/restarting.html", {"kind": kind, "active": "system"})
        page.background = BackgroundTask(request.app.state.restart)
        return page

    @router.post("/system/secrets/clear")
    async def system_clear_secrets(request: Request, session: Admin) -> Response:
        form = await read_form(request)
        state = request.app.state
        if not await reauth(request, session, form, "secrets.clear"):
            return system_page(request, REAUTH_FAILED_MESSAGE, 403)
        await run_in_threadpool(
            admin_ai.clear_secrets, state.engine, state.auth_clock, actor(request, session)
        )
        return back("/admin/system", "secrets_cleared")

    # ---- appearance ---------------------------------------------------------------

    def appearance_page(
        request: Request,
        errors: dict[str, str] | None = None,
        form: dict[str, str] | None = None,
        status: int = 200,
    ) -> Response:
        state = request.app.state
        with state.engine.connect() as conn:
            config = instance.read(conn)
        return render(
            request,
            "admin/appearance.html",
            {
                "palettes": list(setup_steps.PALETTES.items()),
                "values": form
                or {
                    "app_name": config.app_name if config else "WeightPicks",
                    "palette": config.palette if config else "ember",
                },
                "errors": errors or {},
                "has_logo": appearance.has_logo(state.settings),
                "active": "appearance",
            },
            status,
        )

    @router.get("/appearance")
    def appearance_index(request: Request, session: Admin) -> Response:
        return appearance_page(request)

    @router.post("/appearance")
    async def appearance_save(request: Request, session: Admin) -> Response:
        form = dict(await read_form(request))
        state = request.app.state
        errors = await run_in_threadpool(
            appearance.set_name_and_palette,
            state.engine,
            state.auth_clock,
            actor(request, session),
            form,
        )
        if errors:
            return appearance_page(request, errors, form, 400)
        return back("/admin/appearance", "appearance")

    @router.post("/appearance/logo")
    async def appearance_logo(request: Request, session: Admin) -> Response:
        state = request.app.state
        form = await request.form(max_files=1, max_fields=10)
        fields = {k: v for k, v in form.items() if isinstance(v, str)}
        if not await reauth(request, session, fields, "appearance.logo"):
            return appearance_page(request, {"logo": REAUTH_FAILED_MESSAGE}, status=403)
        upload = form.get("file")
        if upload is None or isinstance(upload, str):
            return appearance_page(request, {"logo": "Choose an image file."}, status=400)
        data = await upload.read(appearance.MAX_BYTES + 1)
        try:
            await run_in_threadpool(
                appearance.save_logo,
                state.engine,
                state.settings,
                state.auth_clock,
                actor(request, session),
                data,
            )
        except appearance.AppearanceError as exc:
            return appearance_page(request, {"logo": exc.message}, status=400)
        return back("/admin/appearance", "logo")

    @router.post("/appearance/logo/remove")
    async def appearance_logo_remove(request: Request, session: Admin) -> Response:
        form = await read_form(request)
        state = request.app.state
        if not await reauth(request, session, form, "appearance.logo_removed"):
            return appearance_page(request, {"logo": REAUTH_FAILED_MESSAGE}, status=403)
        await run_in_threadpool(
            appearance.remove_logo,
            state.engine,
            state.settings,
            state.auth_clock,
            actor(request, session),
        )
        return back("/admin/appearance", "logo_removed")
