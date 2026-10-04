"""Outbox -> Discord against a local fake Discord (D-040)."""

from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, select, update

from app.core.clock import SimClock
from app.core.config import Settings
from app.core.db import immediate
from app.models import InstanceSettingsRow, OutboxMessage
from app.notify.dispatcher import MAX_ATTEMPTS, Dispatcher, RateLimiter
from app.services import instance, secrets
from app.services.outbox import Category, enqueue
from app.worker.jobs.outbox import OutboxDispatchJob
from app.worker.registry import run_due
from tests.fake_discord import FakeDiscord, running
from tests.integration.world import local

KEY = "k" * 48


class FakeTime:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def discord() -> Iterator[FakeDiscord]:
    with running() as fake:
        yield fake


@pytest.fixture
def env(migrated_engine: Engine, settings: Settings) -> tuple[Engine, Settings, SimClock]:
    keyed = settings.model_copy(update={"app_secret_key": SecretStr(KEY)})
    clock = SimClock(local(2026, 10, 5, 12))
    with immediate(migrated_engine) as conn:
        instance.ensure(conn, clock, keyed)
    return migrated_engine, keyed, clock


def hook(engine: Engine, clock: SimClock, category: str, url: str) -> None:
    with immediate(engine) as conn:
        secrets.put(conn, clock, KEY, f"webhook.{category}", url)


def public(engine: Engine, clock: SimClock, on: bool = True) -> None:
    with immediate(engine) as conn:
        conn.execute(update(InstanceSettingsRow).values(flags={"discord_public": on}))


def queue(engine: Engine, clock: SimClock, category: str, key: str, **payload: object) -> None:
    with immediate(engine) as conn:
        enqueue(conn, clock, category=Category(category), payload=dict(payload), dedupe_key=key)


def rows(engine: Engine) -> list[Any]:
    with engine.connect() as conn:
        return list(conn.execute(select(OutboxMessage).order_by(OutboxMessage.id)).all())


def dispatcher(settings: Settings, clock: SimClock, ticks: FakeTime | None = None) -> Dispatcher:
    limiter = RateLimiter(monotonic=ticks or FakeTime())
    return Dispatcher(settings, clock, clock, httpx.Client(timeout=5), limiter)


def test_sends_once_and_never_pings(
    env: tuple[Engine, Settings, SimClock], discord: FakeDiscord
) -> None:
    engine, settings, clock = env
    hook(engine, clock, "busts", discord.url())
    public(engine, clock)
    queue(engine, clock, "busts", "bust:1", user_id=None, season_busts=1)
    d = dispatcher(settings, clock)
    assert d.run_pass(engine).sent == 1
    assert d.run_pass(engine).sent == 0  # sent rows are never picked again
    assert len(discord.requests) == 1
    request = discord.requests[0]
    assert request["path"] == "/api/webhooks/1/token?wait=true"
    assert request["body"]["allowed_mentions"] == {"parse": []}
    (row,) = rows(engine)
    assert row.status == "sent" and row.sent_at is not None


def test_blank_webhook_and_public_flag(
    env: tuple[Engine, Settings, SimClock], discord: FakeDiscord
) -> None:
    engine, settings, clock = env
    hook(engine, clock, "admin_alerts", discord.url("2/a"))
    hook(engine, clock, "busts", discord.url("3/b"))
    queue(engine, clock, "admin_alerts", "a1", kind="sync_failed", error="x")
    queue(engine, clock, "busts", "b1", user_id=None)  # discord_public is off
    queue(engine, clock, "bet_results", "r1", result="won")  # no webhook
    result = dispatcher(settings, clock).run_pass(engine)
    assert (result.sent, result.skipped) == (1, 2)
    status = {r.dedupe_key: (r.status, r.last_error) for r in rows(engine)}
    assert status["a1"] == ("sent", None)
    assert status["b1"] == ("skipped", "discord_public is off")
    assert status["r1"] == ("skipped", "no webhook for this category")
    assert [r["path"] for r in discord.requests] == ["/api/webhooks/2/a?wait=true"]


def test_429_waits_without_using_an_attempt(
    env: tuple[Engine, Settings, SimClock], discord: FakeDiscord
) -> None:
    engine, settings, clock = env
    hook(engine, clock, "admin_alerts", discord.url())
    queue(engine, clock, "admin_alerts", "a1", kind="x")
    discord.respond((429, {"retry_after": 3.5, "global": False}))
    ticks = FakeTime()
    d = dispatcher(settings, clock, ticks)
    first = d.run_pass(engine)
    assert first.waiting == 1 and first.more
    (row,) = rows(engine)
    assert row.status == "pending" and row.attempts == 0
    assert row.next_attempt_at == clock.now() + timedelta(seconds=3.5)
    assert d.run_pass(engine).sent == 0  # not due yet
    clock.advance(timedelta(seconds=4))
    ticks.t += 4
    assert d.run_pass(engine).sent == 1
    assert len(discord.requests) == 2


def test_5xx_backs_off_then_goes_dead_with_one_alert(
    env: tuple[Engine, Settings, SimClock], discord: FakeDiscord
) -> None:
    engine, settings, clock = env
    hook(engine, clock, "busts", discord.url())
    public(engine, clock)
    queue(engine, clock, "busts", "b1", user_id=None)
    discord.respond(*[(503, None)] * MAX_ATTEMPTS)
    ticks = FakeTime()
    d = dispatcher(settings, clock, ticks)
    gaps = []
    for _ in range(MAX_ATTEMPTS):
        d.run_pass(engine)
        row = next(r for r in rows(engine) if r.dedupe_key == "b1")
        if row.status == "dead":
            break
        gaps.append(row.next_attempt_at - clock.now())
        clock.advance(row.next_attempt_at - clock.now())
        ticks.t += 3600
    assert gaps[:5] == [
        timedelta(seconds=30),
        timedelta(minutes=2),
        timedelta(minutes=10),
        timedelta(hours=1),
        timedelta(hours=1),
    ]
    dead = next(r for r in rows(engine) if r.dedupe_key == "b1")
    assert dead.status == "dead" and dead.attempts == MAX_ATTEMPTS
    assert "503" in (dead.last_error or "")
    alerts = [r for r in rows(engine) if r.category == "admin_alerts"]
    assert len(alerts) == 1 and alerts[0].payload["kind"] == "delivery_failed"


def test_deleted_webhook_is_dead_at_once_and_urls_never_leak(
    env: tuple[Engine, Settings, SimClock], discord: FakeDiscord
) -> None:
    engine, settings, clock = env
    url = discord.url("99/secret-token-value")
    hook(engine, clock, "busts", url)
    public(engine, clock)
    queue(engine, clock, "busts", "b1", user_id=None)
    discord.respond((404, {"message": "Unknown Webhook"}))
    assert dispatcher(settings, clock).run_pass(engine).dead == 1
    for r in rows(engine):
        assert "secret-token-value" not in str(r.last_error) + str(r.payload)


def test_network_error_retries(env: tuple[Engine, Settings, SimClock]) -> None:
    engine, settings, clock = env
    hook(engine, clock, "admin_alerts", "http://127.0.0.1:9/api/webhooks/1/x")  # nothing listens
    queue(engine, clock, "admin_alerts", "a1", kind="x")
    assert dispatcher(settings, clock).run_pass(engine).retried == 1
    (row,) = rows(engine)
    assert row.attempts == 1 and row.last_error == "network error: ConnectError"


def test_stale_rows_are_skipped(
    env: tuple[Engine, Settings, SimClock], discord: FakeDiscord
) -> None:
    engine, settings, clock = env
    hook(engine, clock, "admin_alerts", discord.url())
    queue(engine, clock, "admin_alerts", "old", kind="x")
    clock.advance(timedelta(hours=25))
    queue(engine, clock, "admin_alerts", "new", kind="x")
    result = dispatcher(settings, clock).run_pass(engine)
    assert (result.sent, result.skipped) == (1, 1)
    assert {r.dedupe_key: r.status for r in rows(engine)} == {"old": "skipped", "new": "sent"}


def test_a_60_message_burst_is_paced(
    env: tuple[Engine, Settings, SimClock], discord: FakeDiscord
) -> None:
    """Stage risk: a big settlement batch. 5 per 2 s and 30 per minute per webhook."""
    engine, settings, clock = env
    hook(engine, clock, "bet_results", discord.url())
    public(engine, clock)
    for i in range(60):
        queue(engine, clock, "bet_results", f"r{i}", result="lost", stake_cents=100)
    ticks = FakeTime()
    d = dispatcher(settings, clock, ticks)
    first = d.run_pass(engine)
    assert first.sent == 5 and first.more
    sent = 5
    for _ in range(11):  # 22 more seconds
        ticks.t += 2
        sent += d.run_pass(engine).sent
    assert sent == 30  # the per-minute ceiling
    ticks.t += 60
    for _ in range(12):
        ticks.t += 2
        sent += d.run_pass(engine).sent
    assert sent == 60 and len(discord.requests) == 60
    assert len({r["body"]["embeds"][0]["description"] for r in discord.requests}) >= 1


def test_without_a_secret_key_nothing_is_sent(
    migrated_engine: Engine, settings: Settings, discord: FakeDiscord
) -> None:
    clock = SimClock(local(2026, 10, 5, 12))
    with immediate(migrated_engine) as conn:
        instance.ensure(conn, clock, settings)
    queue(migrated_engine, clock, "admin_alerts", "a1", kind="x")
    result = dispatcher(settings, clock).run_pass(migrated_engine)
    assert result.skipped == 1 and discord.requests == []


def test_worker_job_drives_the_fast_loop(
    env: tuple[Engine, Settings, SimClock], discord: FakeDiscord
) -> None:
    engine, settings, clock = env
    hook(engine, clock, "admin_alerts", discord.url())
    for i in range(7):
        queue(engine, clock, "admin_alerts", f"a{i}", kind="x")
    job = OutboxDispatchJob(dispatcher(settings, clock))
    assert run_due([job], engine, clock) == ["outbox_dispatch"]
    assert job.more  # 5 sent, 2 held back by the 5-per-2-s bucket
    assert len(discord.requests) == 5


def test_admin_test_posts_go_even_while_public_posts_are_off(
    env: tuple[Engine, Settings, SimClock], discord: FakeDiscord
) -> None:
    engine, settings, clock = env
    hook(engine, clock, "busts", discord.url())
    queue(engine, clock, "busts", "test:busts:1", kind="test")
    queue(engine, clock, "busts", "b1", user_id=None)
    result = dispatcher(settings, clock).run_pass(engine)
    assert (result.sent, result.skipped) == (1, 1)
    assert discord.bodies[0]["embeds"][0]["title"] == "Webhook test"
