"""Shared pieces of the admin routes: rendering, the acting admin, the password re-check."""

from typing import Any

from fastapi import Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from app.core.security import verify_password
from app.models import User
from app.services import admin
from app.services.audit import Actor
from app.services.auth import SessionInfo
from app.web.security import client_ip

MESSAGES = {
    "voided": "Market voided and every open stake refunded.",
    "frozen": "Player frozen: they can log in but not bet.",
    "unfrozen": "Player unfrozen.",
    "banned": "Player removed. Their open bets were refunded and their email is blocked.",
    "bailout": "Bailout paid.",
    "adjusted": "Balance adjusted.",
    "economy": "Economy settings saved.",
    "sync": "Sync requested. The worker picks it up within a minute.",
    "webhook": "Webhook saved.",
    "public_url": "Public URL saved.",
    "timezone": "Time zone changed. Open bets were refunded and players notified.",
    "test": "Test post queued. It should appear in Discord within a minute.",
    "flags": "Settings saved.",
    "prop": "Prop posted. Players can bet on it until tonight's lock.",
    "approved": "AI prop approved: re-priced and posted.",
    "rejected": "AI proposal rejected.",
    "ai_mode": "AI publishing mode saved.",
    "ai_run": "AI run requested. The worker picks it up within a minute.",
    "workers_ai": "Workers AI settings saved.",
    "note": "Note saved.",
    "note_ended": "Note ended.",
    "pool": "Special event published. Players can enter until the night before.",
    "pool_void": "Special event voided and every buy-in refunded.",
    "instance_frozen": "Instance frozen: no bets or pool entries until you unfreeze.",
    "instance_unfrozen": "Instance unfrozen.",
    "goal": "Goal Reached: everything is settled or refunded and the instance is frozen.",
    "season": "New season started. Balances carried over; betting is open again.",
    "backup": "Backup created.",
    "appearance": "Appearance saved.",
    "logo": "Logo saved. It may take a refresh to show everywhere.",
    "logo_removed": "Logo removed: back to the default.",
    "secrets_cleared": "Unreadable secrets cleared. Enter the integrations again in Settings.",
    "smtp": "Email settings saved.",
    "smtp_test": "Test email queued to your address. It should arrive within a minute.",
}
REAUTH_FAILED_MESSAGE = "That's not your password. Nothing was changed."


def render(request: Request, name: str, context: dict[str, Any], status: int = 200) -> Response:
    session: SessionInfo = request.state.session
    base = {
        "session": session,
        "csrf": session.csrf_token,
        "admin_nav": True,
        "message": MESSAGES.get(request.query_params.get("ok", "")),
    }
    page: Response = request.app.state.templates.TemplateResponse(
        request, name, base | context, status_code=status
    )
    return page


def actor(request: Request, session: SessionInfo) -> Actor:
    return Actor(session.user_id, client_ip(request), request.app.state.auth_clock.now())


async def reauth(request: Request, session: SessionInfo, form: dict[str, str], action: str) -> bool:
    state = request.app.state
    with state.engine.connect() as conn:
        stored = conn.execute(
            select(User.password_hash).where(User.id == session.user_id)
        ).scalar_one()
    ok = await run_in_threadpool(verify_password, stored, form.get("admin_password", ""))
    if not ok:
        await run_in_threadpool(
            admin.reauth_failed, state.engine, state.auth_clock, actor(request, session), action
        )
    return bool(ok)


def back(path: str, ok: str) -> Response:
    return RedirectResponse(f"{path}?ok={ok}", status_code=303)
