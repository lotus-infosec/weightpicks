"""Outbox -> Discord (BUILD_PLAN §1.4.5, D-040).

One pass reads due rows, decides each one (skip, wait, send), and makes the HTTP call
with no database transaction open; each outcome is written in its own short write.
Webhook URLs are secrets: they are decrypted per pass and never logged or stored in
an error.
"""

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import httpx
import structlog
from cryptography.fernet import InvalidToken
from sqlalchemy import Engine, or_, select, update

from app.core.clock import Clock
from app.core.config import Settings
from app.core.crypto import SecretKeyMissing, derive_fernet
from app.core.db import immediate
from app.models import Market, OutboxMessage, User
from app.notify import email, embeds
from app.services import instance, secrets
from app.services.outbox import Category, enqueue

log = structlog.get_logger()
BATCH = 50
MAX_ATTEMPTS = 8
BACKOFF = (timedelta(seconds=30), timedelta(minutes=2), timedelta(minutes=10), timedelta(hours=1))
STALE_AFTER = timedelta(hours=24)  # domain time; stops a sim fast-forward flooding Discord
BURST, BURST_WINDOW = 5, 2.0  # per webhook URL
PER_MINUTE = 30  # one channel per webhook
TIMEOUT = 10.0
REMOVED_PLAYER = "Removed player"


@dataclass(slots=True)
class RateLimiter:
    """Token bucket per webhook URL (5 per 2 s) plus a 30-per-minute channel ceiling and
    any `retry_after` Discord asked for. In memory: one worker process sends."""

    monotonic: Callable[[], float] = time.monotonic
    sent: dict[str, deque[float]] = field(default_factory=dict)
    blocked_until: dict[str, float] = field(default_factory=dict)

    def allow(self, url: str) -> bool:
        now = self.monotonic()
        if self.blocked_until.get(url, 0.0) > now:
            return False
        history = self.sent.setdefault(url, deque())
        while history and now - history[0] >= 60.0:
            history.popleft()
        recent = sum(1 for t in history if now - t < BURST_WINDOW)
        return recent < BURST and len(history) < PER_MINUTE

    def record(self, url: str) -> None:
        self.sent.setdefault(url, deque()).append(self.monotonic())

    def block(self, url: str, seconds: float) -> None:
        self.blocked_until[url] = self.monotonic() + seconds


@dataclass(slots=True)
class PassResult:
    sent: int = 0
    skipped: int = 0
    retried: int = 0
    dead: int = 0
    waiting: int = 0  # due but held back by the rate limiter

    @property
    def more(self) -> bool:
        """Due rows remain: some were rate-limited or the batch was full."""
        return self.waiting > 0 or self.sent + self.skipped + self.retried + self.dead >= BATCH


def _webhooks(conn: Any, settings: Settings) -> dict[str, str]:
    key = settings.app_secret_key.get_secret_value()
    if not key:
        return {}
    found: dict[str, str] = {}
    for category in secrets.WEBHOOK_CATEGORIES:
        url = secrets.get(conn, key, f"webhook.{category}")
        if url:
            found[category] = url
    return found


def _context(conn: Any, rows: list[Any], app_name: str, base_url: str, now: datetime) -> Any:
    user_ids = {r.payload.get("user_id") for r in rows}
    for r in rows:
        user_ids |= {row.get("user_id") for row in r.payload.get("rows", [])}
    market_ids = {r.payload.get("market_id") for r in rows}
    users = {
        uid: (REMOVED_PLAYER if status == "banned" else name)
        for uid, name, status in conn.execute(
            select(User.id, User.display_name, User.status).where(
                User.id.in_([u for u in user_ids if u])
            )
        )
    }
    titles = dict(
        conn.execute(
            select(Market.id, Market.title).where(Market.id.in_([m for m in market_ids if m]))
        ).all()
    )
    return embeds.Context(
        app_name=app_name,
        base_url=base_url,
        now=now,
        user_name=lambda uid: users.get(uid, "A player"),
        market_title=lambda mid: titles.get(mid, "a market"),
    )


def _retry_after(response: httpx.Response) -> float:
    try:
        value = float(response.json().get("retry_after", 0))
    except (ValueError, AttributeError):
        value = 0.0
    if value <= 0:
        try:
            value = float(response.headers.get("retry-after", "1"))
        except ValueError:
            value = 1.0
    return min(max(value, 0.5), 3600.0)


class Dispatcher:
    def __init__(
        self,
        settings: Settings,
        clock: Clock,
        domain_clock: Clock,
        client: httpx.Client | None = None,
        limiter: RateLimiter | None = None,
    ) -> None:
        self.settings = settings
        self.clock = clock  # real time: retries and rate limits
        self.domain_clock = domain_clock  # outbox rows are stamped with app time
        self.client = client or httpx.Client(timeout=TIMEOUT, follow_redirects=False)
        self.limiter = limiter or RateLimiter()

    def run_pass(self, engine: Engine) -> PassResult:
        result = PassResult()
        now = self.clock.now()
        self._email_pass(engine, result, now)
        with engine.connect() as conn:
            rows = conn.execute(
                select(OutboxMessage)
                .where(
                    OutboxMessage.channel == "discord",
                    OutboxMessage.status == "pending",
                    or_(
                        OutboxMessage.next_attempt_at <= now,
                        (OutboxMessage.attempts == 0) & OutboxMessage.last_error.is_(None),
                    ),
                )
                .order_by(OutboxMessage.id)
                .limit(BATCH)
            ).all()
            if not rows:
                return result
            config = instance.read(conn)
            hooks = _webhooks(conn, self.settings)
            ctx = _context(
                conn,
                list(rows),
                config.app_name if config else "WeightPicks",
                self.settings.wp_base_url,
                now,
            )
        public = bool(config and config.flags.get("discord_public"))
        stale_before = self.domain_clock.now() - STALE_AFTER
        for row in rows:
            url = hooks.get(row.category)
            is_test = row.payload.get("kind") == "test"
            reason = None
            if url is None:
                reason = "no webhook for this category"
            elif not public and row.category != Category.ADMIN_ALERTS and not is_test:
                reason = "discord_public is off"  # the admin's own test posts always go
            elif row.created_at < stale_before and not is_test:
                reason = "stale (older than 24 h)"
            if reason or url is None:
                self._finish(engine, row.id, "skipped", reason)
                result.skipped += 1
                continue
            if not self.limiter.allow(url):
                result.waiting += 1
                continue
            body = embeds.build(row.category, row.payload, ctx)
            self._send(engine, row, url, body, result)
        return result

    # ---- email (STAGE15): password resets and SMTP tests --------------------------------

    def _email_pass(self, engine: Engine, result: PassResult, now: datetime) -> None:
        with engine.connect() as conn:
            rows = conn.execute(
                select(OutboxMessage)
                .where(
                    OutboxMessage.channel == "email",
                    OutboxMessage.status == "pending",
                    OutboxMessage.next_attempt_at <= now,
                )
                .order_by(OutboxMessage.id)
                .limit(BATCH)
            ).all()
            if not rows:
                return
            cfg = email.load(conn, self.settings)
            config = instance.read(conn)
            users = {
                uid: (addr, name, status)
                for uid, addr, name, status in conn.execute(
                    select(User.id, User.email, User.display_name, User.status).where(
                        User.id.in_({r.payload.get("user_id") for r in rows})
                    )
                )
            }
        app_name = config.app_name if config else "WeightPicks"
        for row in rows:
            user = users.get(row.payload.get("user_id"))
            if cfg is None:
                self._finish(engine, row.id, "skipped", "SMTP isn't set up")
                result.skipped += 1
                continue
            if user is None or user[2] == "banned":
                self._finish(engine, row.id, "skipped", "no such account")
                result.skipped += 1
                continue
            try:
                msg = self._email_message(row.payload, cfg, app_name, user[0], user[1])
            except ValueError as exc:
                self._dead(engine, row, str(exc), result)
                continue
            try:
                email.send(cfg, msg)
            except email.EmailError as exc:
                if exc.permanent:
                    self._dead(engine, row, exc.reason, result)
                else:
                    self._retry(engine, row, exc.reason, result)
                continue
            clean = {k: v for k, v in row.payload.items() if k != "sealed"}  # drop the token
            with immediate(engine) as conn:
                conn.execute(
                    update(OutboxMessage)
                    .where(OutboxMessage.id == row.id)
                    .values(status="sent", payload=clean, last_error=None, sent_at=self.clock.now())
                )
            result.sent += 1
            log.info("email_sent", outbox_id=row.id, kind=row.payload.get("kind"))

    def _email_message(
        self, payload: dict[str, Any], cfg: Any, app_name: str, to: str, name: str
    ) -> Any:
        kind = payload.get("kind")
        base = self.settings.wp_base_url.rstrip("/")
        if kind == "password_reset":
            try:
                token = (
                    derive_fernet(self.settings.app_secret_key.get_secret_value())
                    .decrypt(str(payload["sealed"]).encode())
                    .decode()
                )
            except (KeyError, InvalidToken, SecretKeyMissing) as exc:
                raise ValueError("the reset link can't be read (APP_SECRET_KEY changed?)") from exc
            text = (
                f"Hi {name},\n\nSomeone (hopefully you) asked to reset your {app_name} password. "
                f"Open this link within an hour to choose a new one:\n\n{base}/reset/{token}\n\n"
                "If you didn't ask, ignore this email: your password stays the same.\n"
            )
            return email.build(cfg, app_name, to, f"Reset your {app_name} password", text)
        if kind == "smtp_test":
            text = f"Hi {name},\n\nThis is a test email from {app_name}. Email works.\n"
            return email.build(cfg, app_name, to, f"{app_name}: test email", text)
        raise ValueError(f"unknown email kind {kind!r}")

    def _send(
        self, engine: Engine, row: Any, url: str, body: dict[str, Any], result: PassResult
    ) -> None:
        self.limiter.record(url)
        try:
            response = self.client.post(url, params={"wait": "true"}, json=body)
        except httpx.HTTPError as exc:
            self._retry(engine, row, f"network error: {type(exc).__name__}", result)
            return
        status = response.status_code
        if 200 <= status < 300:
            self._finish(engine, row.id, "sent", None)
            result.sent += 1
        elif status == 429:
            wait = _retry_after(response)
            self.limiter.block(url, wait)
            with immediate(engine) as conn:  # does not use up an attempt
                conn.execute(
                    update(OutboxMessage)
                    .where(OutboxMessage.id == row.id)
                    .values(
                        next_attempt_at=self.clock.now() + timedelta(seconds=wait),
                        last_error=f"rate limited by Discord (retry after {wait:.1f} s)",
                    )
                )
            result.waiting += 1
        elif status >= 500:
            self._retry(engine, row, f"Discord returned {status}", result)
        else:  # 4xx: the webhook was deleted or the body was rejected; retrying won't help
            self._dead(engine, row, f"Discord returned {status}", result)

    def _retry(self, engine: Engine, row: Any, error: str, result: PassResult) -> None:
        attempts = row.attempts + 1
        if attempts >= MAX_ATTEMPTS:
            self._dead(engine, row, f"{error} after {attempts} attempts", result)
            return
        delay = BACKOFF[min(attempts - 1, len(BACKOFF) - 1)]
        with immediate(engine) as conn:
            conn.execute(
                update(OutboxMessage)
                .where(OutboxMessage.id == row.id)
                .values(
                    attempts=attempts,
                    next_attempt_at=self.clock.now() + delay,
                    last_error=error,
                )
            )
        result.retried += 1

    def _dead(self, engine: Engine, row: Any, error: str, result: PassResult) -> None:
        with immediate(engine) as conn:
            conn.execute(
                update(OutboxMessage)
                .where(OutboxMessage.id == row.id)
                .values(status="dead", attempts=row.attempts + 1, last_error=error)
            )
            if row.category != Category.ADMIN_ALERTS:  # never alert about an alert
                enqueue(
                    conn,
                    self.domain_clock,
                    category=Category.ADMIN_ALERTS,
                    payload={"kind": "delivery_failed", "category": row.category, "error": error},
                    dedupe_key=f"dead:{row.id}",
                )
        log.warning("outbox_dead", outbox_id=row.id, category=row.category, error=error)
        result.dead += 1

    def _finish(self, engine: Engine, row_id: int, status: str, error: str | None) -> None:
        with immediate(engine) as conn:
            conn.execute(
                update(OutboxMessage)
                .where(OutboxMessage.id == row_id)
                .values(
                    status=status,
                    last_error=error,
                    sent_at=self.clock.now() if status == "sent" else None,
                )
            )
