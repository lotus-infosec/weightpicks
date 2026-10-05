"""Admin controls for Workers AI (D-042): review vs auto-publish, the Workers AI account
id and token (encrypted, never shown back), admin notes for the digest, and the AI runs
view. Every mutation is audited; secrets never reach the audit log."""

from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import Connection, Engine, insert, select, update

from app.ai import quota
from app.core.clock import Clock
from app.core.crypto import SecretKeyMissing, WrongSecretKey
from app.core.db import immediate
from app.models import AdminNote, AiRun, InstanceSettingsRow
from app.services import audit
from app.services import secrets as secret_store
from app.services.admin import Actor, AdminError

AI_MODES = ("review", "auto")
NOTE_MAX = 200
NOTE_MAX_DAYS = 60
SECRET_NAMES = ("workers_ai.account_id", "workers_ai.token")


def set_ai_mode(engine: Engine, clock: Clock, actor: Actor, mode: str) -> None:
    if mode not in AI_MODES:
        raise AdminError("bad_mode", "Pick review or auto-publish.")
    with immediate(engine) as conn:
        before = conn.execute(select(InstanceSettingsRow.ai_mode)).scalar_one()
        if before == mode:
            return
        conn.execute(
            update(InstanceSettingsRow)
            .where(InstanceSettingsRow.id == 1)
            .values(ai_mode=mode, updated_at=clock.now())
        )
        audit.record(
            conn,
            clock,
            actor_id=actor.user_id,
            ts=actor.acted_at,
            action="settings.ai_mode",
            target=("settings", 1),
            before={"ai_mode": before},
            after={"ai_mode": mode},
            ip=actor.ip,
        )


def set_workers_ai(
    engine: Engine,
    clock: Clock,
    actor: Actor,
    app_secret_key: str,
    account_id: str,
    token: str,
    clear: bool = False,
) -> None:
    """Store both values (or clear both). Blank fields keep what is stored."""
    account_id, token = account_id.strip(), token.strip()
    if not clear and not (account_id or token):
        raise AdminError("empty", "Enter the account ID and token, or tick Clear.")
    if len(account_id) > 64 or len(token) > 200 or any(c.isspace() for c in account_id + token):
        raise AdminError("bad_value", "That doesn't look like a Cloudflare account ID or token.")
    with immediate(engine) as conn:
        if clear:
            for name in SECRET_NAMES:
                secret_store.remove(conn, name)
        else:
            try:
                for name, value in zip(SECRET_NAMES, (account_id, token), strict=True):
                    if value:
                        secret_store.put(conn, clock, app_secret_key, name, value)
            except (SecretKeyMissing, WrongSecretKey) as exc:
                raise AdminError("no_key", "APP_SECRET_KEY is missing or wrong.") from exc
        audit.record(
            conn,
            clock,
            actor_id=actor.user_id,
            ts=actor.acted_at,
            action="integrations.workers_ai",
            target=("settings", 1),
            after={
                "cleared": clear,
                "account_id_changed": bool(account_id) and not clear,
                "credential_changed": bool(token) and not clear,
            },
            ip=actor.ip,
        )


def workers_ai_configured(conn: Connection) -> bool:
    return set(SECRET_NAMES) <= set(secret_store.names(conn))


def add_note(
    engine: Engine, clock: Clock, actor: Actor, text: str, active_from: date, active_to: date
) -> int:
    text = " ".join(text.split())
    if not text or len(text) > NOTE_MAX:
        raise AdminError("bad_note", f"A note is 1 to {NOTE_MAX} characters.")
    if active_to < active_from or (active_to - active_from).days > NOTE_MAX_DAYS:
        raise AdminError(
            "bad_dates", f"The end date is on or after the start, within {NOTE_MAX_DAYS} days."
        )
    with immediate(engine) as conn:
        note_id: int = conn.execute(
            insert(AdminNote)
            .values(
                text=text,
                active_from=active_from,
                active_to=active_to,
                created_at=clock.now(),
                created_by=actor.user_id,
            )
            .returning(AdminNote.id)
        ).scalar_one()
        audit.record(
            conn,
            clock,
            actor_id=actor.user_id,
            ts=actor.acted_at,
            action="note.add",
            target=("admin_note", note_id),
            after={"text": text, "from": active_from.isoformat(), "to": active_to.isoformat()},
            ip=actor.ip,
        )
    return note_id


def end_note(engine: Engine, clock: Clock, actor: Actor, note_id: int, today: date) -> None:
    """Stop a note from today on: it ends yesterday (or the day before it starts)."""
    with immediate(engine) as conn:
        row = conn.execute(select(AdminNote).where(AdminNote.id == note_id)).one_or_none()
        if row is None:
            raise AdminError("not_found", "That note doesn't exist.")
        new_to = min(row.active_to, max(today, row.active_from) - timedelta(days=1))
        if new_to == row.active_to:
            return
        conn.execute(update(AdminNote).where(AdminNote.id == note_id).values(active_to=new_to))
        audit.record(
            conn,
            clock,
            actor_id=actor.user_id,
            ts=actor.acted_at,
            action="note.end",
            target=("admin_note", note_id),
            before={"to": row.active_to.isoformat()},
            after={"to": new_to.isoformat()},
            ip=actor.ip,
        )


def notes(conn: Connection, today: date) -> list[dict[str, Any]]:
    rows = conn.execute(
        select(AdminNote).order_by(AdminNote.active_to.desc(), AdminNote.id.desc()).limit(50)
    )
    return [
        {
            "id": r.id,
            "text": r.text,
            "from": r.active_from,
            "to": r.active_to,
            "state": "active"
            if r.active_from <= today <= r.active_to
            else ("upcoming" if r.active_from > today else "ended"),
        }
        for r in rows
    ]


def runs_view(conn: Connection, real_now: datetime, cap: int, limit: int = 50) -> dict[str, Any]:
    rows = conn.execute(select(AiRun).order_by(AiRun.id.desc()).limit(limit)).all()
    return {"used": quota.used_today(conn, real_now), "cap": cap, "runs": rows}


def audit_simple(
    engine: Engine, clock: Clock, actor: Actor, action: str, after: dict[str, Any]
) -> None:
    with immediate(engine) as conn:
        audit.record(
            conn,
            clock,
            actor_id=actor.user_id,
            ts=actor.acted_at,
            ip=actor.ip,
            action=action,
            after=after,
        )


def clear_secrets(engine: Engine, clock: Clock, actor: Actor) -> int:
    """Delete every stored secret (they can't be decrypted with this APP_SECRET_KEY)."""
    with immediate(engine) as conn:
        removed = secret_store.clear_all(conn)
        audit.record(
            conn,
            clock,
            actor_id=actor.user_id,
            ts=actor.acted_at,
            ip=actor.ip,
            action="secrets.clear",
            after={"removed": removed},
        )
    return removed


# ---- SMTP (STAGE15) -------------------------------------------------------------------------


def set_smtp(
    engine: Engine,
    clock: Clock,
    actor: Actor,
    app_secret_key: str,
    form: dict[str, str],
    clear: bool = False,
) -> dict[str, str]:
    """Save (or clear) the SMTP settings; the password is stored encrypted and a blank
    password keeps the stored one. Returns field errors ({} = saved)."""
    from app.domain import setup as setup_rules
    from app.models import InstanceSettingsRow

    values: dict[str, Any] = {"configured": False}
    if not clear:
        values, errors = setup_rules.smtp(form)
        if errors:
            return errors
        if not values.get("configured"):
            return {"host": "Enter the SMTP server, or tick Clear to turn email off."}
    with immediate(engine) as conn:
        conn.execute(
            update(InstanceSettingsRow)
            .where(InstanceSettingsRow.id == 1)
            .values(smtp=values, updated_at=clock.now())
        )
        password = form.get("password", "")
        if clear:
            secret_store.remove(conn, "smtp.password")
        elif password:
            try:
                secret_store.put(conn, clock, app_secret_key, "smtp.password", password)
            except (SecretKeyMissing, WrongSecretKey) as exc:
                raise AdminError("no_key", "APP_SECRET_KEY is missing or wrong.") from exc
        audit.record(
            conn,
            clock,
            actor_id=actor.user_id,
            ts=actor.acted_at,
            ip=actor.ip,
            action="integrations.smtp",
            target=("settings", 1),
            after={k: v for k, v in values.items() if k != "username"}
            | {"credential_changed": bool(password) and not clear},
        )
    return {}


def send_smtp_test(engine: Engine, clock: Clock, actor: Actor) -> bool:
    """Queue a test email to the admin's own address. False if SMTP isn't set up."""
    from secrets import token_hex

    from app.notify import email
    from app.services.outbox import Category, enqueue

    with immediate(engine) as conn:
        if not email.configured(conn):
            return False
        enqueue(
            conn,
            clock,
            category=Category.ACCOUNT_EMAIL,
            channel="email",
            payload={"kind": "smtp_test", "user_id": actor.user_id},
            dedupe_key=f"smtp_test:{clock.now().isoformat()}:{token_hex(4)}",
        )
    return True
