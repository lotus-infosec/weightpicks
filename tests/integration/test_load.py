"""Load test: 50 virtual users place bets over HTTP against a real uvicorn server
while settlement, the dispatcher and ledger checks run on the same SQLite file.

Pass: no 5xx, no `database is locked`, the settled market paid every pre-placed bet
exactly once, and the ledger verifies. `LOAD_SECONDS` sets the betting time (default 20);
`LOAD_DUMP=<file>` keeps the server output.
"""

import os
import re
import socket
import threading
import time
from collections import Counter
from datetime import date
from pathlib import Path

import httpx
import pytest
import uvicorn
from sqlalchemy import func, select, update

from app.core.db import immediate
from app.core.security import hash_password
from app.models import Bet, Settlement, User
from app.notify.dispatcher import Dispatcher
from app.services import ledger, settlement
from app.web.main import create_app
from tests.integration import web
from tests.integration.test_bets_settlement import (
    bet,
    player,
    selection,
    to_settle_time,
    weight_market,
)
from tests.integration.world import World, local

pytestmark = pytest.mark.load

USERS = 50
PRE_PLACED = 10  # bets each user already has on the market that settles mid-run
SECONDS = float(os.environ.get("LOAD_SECONDS", "20"))
_META = re.compile(r'<meta name="csrf-token" content="([^"]*)"')


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_fifty_bettors_while_settlement_runs(
    world: World, capfd: pytest.CaptureFixture[str]
) -> None:
    w = world
    w.clock.set(local(2026, 10, 5, 12))
    to_settle_now = weight_market(w, 0, day=date(2026, 10, 5))  # settles on Oct 6's weigh-in
    still_open = weight_market(w, 0, day=date(2026, 10, 8))  # open for the whole run
    users = [player(w, n) for n in range(1, USERS + 1)]
    for u in users:
        for k in range(PRE_PLACED):
            bet(w, u, to_settle_now, "over" if k % 2 else "under", stake=100, key=f"pre-{u}-{k}")
    with immediate(w.engine) as conn:  # the settlement helpers' players get a password
        conn.execute(
            update(User).where(User.id.in_(users)).values(password_hash=hash_password(web.PASSWORD))
        )

    settings = w.settings.model_copy(update={"wp_cookie_secure": False})
    app = create_app(settings, domain_clock=w.clock)
    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started

    statuses: Counter[int] = Counter()
    problems: list[str] = []
    lock = threading.Lock()
    stop = threading.Event()
    sel, ver = selection(w, still_open, "over")

    def bettor(n: int) -> None:
        ip = f"10.1.{n // 250}.{n % 250 + 1}"  # one client IP each (the login limit is per IP)
        with httpx.Client(
            base_url=f"http://127.0.0.1:{port}", headers={"CF-Connecting-IP": ip}, timeout=30
        ) as c:
            token = re.search(r'name="csrf_token" value="([^"]+)"', c.get("/login").text)
            assert token
            r = c.post(
                "/api/auth/login",
                data={
                    "csrf_token": token.group(1),
                    "email": f"p{n}@example.invalid",
                    "password": web.PASSWORD,
                },
            )
            meta = _META.search(c.get("/").text)
            if r.status_code >= 400 or not meta:
                with lock:
                    problems.append(f"login {n}: {r.status_code}")
                return
            csrf, k = meta.group(1), 0
            while not stop.is_set():
                k += 1
                r = c.post(
                    "/api/bets",
                    json={
                        "selection_id": sel,
                        "odds_version_id": ver,
                        "stake_cents": 100,
                        "client_key": f"load-{n}-{k}",
                    },
                    headers={"X-CSRF-Token": csrf},
                )
                with lock:
                    statuses[r.status_code] += 1
                    if r.status_code >= 500 or "locked" in r.text.lower():
                        problems.append(f"bet {n}/{k}: {r.status_code} {r.text[:120]}")

    workers = [threading.Thread(target=bettor, args=(n,)) for n in range(1, USERS + 1)]
    for t in workers:
        t.start()
    time.sleep(min(3.0, SECONDS / 4))
    to_settle_time(w)  # the clock moves past Oct 6's weigh-in: the market locks
    result = settlement.settle_market(w.engine, w.clock, to_settle_now)
    dispatcher = Dispatcher(settings, w.clock, w.clock)
    end = time.monotonic() + SECONDS
    passes = 0
    while time.monotonic() < end:
        dispatcher.run_pass(w.engine)  # writes outbox state while bets keep coming
        with w.engine.connect() as conn:
            assert ledger.verify(conn).ok
        passes += 1
    stop.set()
    for t in workers:
        t.join(timeout=60)
    server.should_exit = True
    thread.join(timeout=10)

    output = capfd.readouterr()
    if os.environ.get("LOAD_DUMP"):
        Path(os.environ["LOAD_DUMP"]).write_text(output.out + output.err)
    with w.engine.connect() as conn:
        settled = conn.execute(
            select(func.count())
            .select_from(Settlement)
            .where(Settlement.market_id == to_settle_now)
        ).scalar_one()
        unsettled = conn.execute(
            select(func.count())
            .select_from(Bet)
            .where(Bet.status == "open", Bet.client_key.like("pre-%"))
        ).scalar_one()
        placed = conn.execute(
            select(func.count()).select_from(Bet).where(Bet.client_key.like("load-%"))
        ).scalar_one()
        report = ledger.verify(conn)
    print(
        f"\nload: {USERS} users, {SECONDS:.0f}s, {sum(statuses.values())} requests "
        f"{dict(statuses)}, {placed} bets placed; settled {result.bets} bets mid-run; "
        f"{passes} dispatcher/verify passes; ledger ok={report.ok}"
    )
    assert not problems, problems[:5]
    assert "database is locked" not in output.out + output.err
    assert result.settled and result.bets == USERS * PRE_PLACED
    assert settled == 1 and unsettled == 0
    assert placed == statuses[200] and placed > USERS
    assert set(statuses) <= {200, 409}  # 409: a business refusal, never an error
    assert report.ok
