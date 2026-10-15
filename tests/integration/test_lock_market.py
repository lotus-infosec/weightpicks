"""Issue #60: the admin locks an open market early with a reason players see. New bets
stop; bets already placed stay and settle as usual; a locked market never reopens."""

from datetime import date

import pytest
from httpx import Response
from pydantic import SecretStr
from sqlalchemy import select

from app.core.clock import SystemClock
from app.models import AuditEntry, Market, OutboxMessage
from app.notify import embeds
from app.services import admin, auth, board, settlement
from app.services.admin import AdminError
from app.services.bets import BetRejected, place_parlay
from app.web.main import create_app
from tests.integration import web
from tests.integration.test_bets_settlement import (
    bet,
    change,
    player,
    selection,
    to_settle_time,
    weight_market,
)
from tests.integration.test_season import admin_actor, set_goal
from tests.integration.world import World

NOTE = "Line under review"


def status(w: World, market_id: int) -> str:
    with w.engine.connect() as conn:
        return str(conn.execute(select(Market.status).where(Market.id == market_id)).scalar_one())


def test_lock_stops_new_bets_and_placed_bets_still_settle(world: World) -> None:
    p = player(world, 1)
    market = weight_market(world, change(date(2026, 10, 5), date(2026, 10, 6)) - 5)
    placed = bet(world, p, market, "over")
    admin.lock_market(world.engine, world.clock, admin_actor(world), market, f"  {NOTE}  ")
    assert status(world, market) == "locked"
    with pytest.raises(BetRejected):
        bet(world, player(world, 2), market, "under")
    with world.engine.connect() as conn:
        mine = {b.bet_id: b for b in board.my_bets(conn, p)}
        card = board.market(conn, market)
        audit_row = conn.execute(
            select(AuditEntry.reason, AuditEntry.after).where(AuditEntry.action == "market.lock")
        ).one()
        payload = conn.execute(
            select(OutboxMessage.payload).where(
                OutboxMessage.dedupe_key == f"market_locked:{market}"
            )
        ).scalar_one()
    assert mine[placed.bet_id].reason == f"Locked early by the admin: {NOTE}"
    assert card is not None and card[0].lock_note == NOTE
    assert card[0].lock_at <= world.clock.now()  # "Locked <time>" shows when it happened
    assert audit_row.reason == NOTE and audit_row.after["status"] == "locked"
    assert payload["result"] == "locked" and payload["reason"].endswith(NOTE)

    to_settle_time(world)
    assert settlement.settle_market(world.engine, world.clock, market).reason == "over"
    with world.engine.connect() as conn:
        settled = {b.bet_id: b for b in board.my_bets(conn, p)}[placed.bet_id]
    assert (settled.status, settled.reason) == ("won", None)


def test_only_an_open_market_with_a_reason_locks(world: World) -> None:
    boss = admin_actor(world)
    market = weight_market(world, -5)
    with pytest.raises(AdminError) as info:
        admin.lock_market(world.engine, world.clock, boss, market, "  ")
    assert info.value.code == "reason_required"
    with pytest.raises(AdminError) as info:
        admin.lock_market(world.engine, world.clock, boss, 9999, NOTE)
    assert info.value.code == "not_found"
    admin.lock_market(world.engine, world.clock, boss, market, NOTE)
    with pytest.raises(AdminError) as info:  # locked stays locked: no second lock, no reopen
        admin.lock_market(world.engine, world.clock, boss, market, NOTE)
    assert info.value.code == "not_open"


def test_a_parlay_keeps_its_leg_but_new_parlays_cannot_use_it(world: World) -> None:
    set_goal(world)  # also turns parlays on
    p = player(world, 1)
    keep = weight_market(world, -5, day=date(2026, 10, 7))
    other = weight_market(world, -5, day=date(2026, 10, 9))
    placed = place_parlay(
        world.engine,
        world.clock,
        user_id=p,
        legs=[selection(world, keep, "over"), selection(world, other, "over")],
        stake_cents=1_000,
        client_key="before-lock",
    )
    admin.lock_market(world.engine, world.clock, admin_actor(world), keep, NOTE)
    with world.engine.connect() as conn:
        row = {b.bet_id: b for b in board.my_bets(conn, p)}[placed.bet_id]
    assert row.status == "open" and [leg.status for leg in row.legs] == ["open", "open"]
    with pytest.raises(BetRejected):
        place_parlay(
            world.engine,
            world.clock,
            user_id=p,
            legs=[selection(world, keep, "under"), selection(world, other, "under")],
            stake_cents=1_000,
            client_key="after-lock",
        )


def test_the_discord_post() -> None:
    from tests.unit.test_embeds import ctx

    post = embeds.build(
        "market_settlements",
        {"market_id": 7, "title": "Weight", "result": "locked", "reason": f"Locked: {NOTE}"},
        ctx(),
    )["embeds"][0]
    assert post["title"] == "Market locked early" and NOTE in post["description"]


def test_admin_route_needs_the_password_and_shows_the_note(world: World) -> None:
    world.settings = world.settings.model_copy(update={"app_secret_key": SecretStr("k" * 40)})
    market = weight_market(world, -5)
    page = f"/admin/markets/{market}"
    code = auth.rotate_registration_code(world.engine, SystemClock())
    app = create_app(world.settings, domain_clock=world.clock)
    with web.client(app) as c, web.client(app) as player_c:
        web.as_admin(c, world.engine)
        web.register(player_c, code, 1)
        assert 'data-testid="lock-market"' in c.get(page).text

        def post(**data: str) -> Response:
            token = web.page_csrf(c, page)
            response: Response = c.post(
                f"{page}/lock", data={"csrf_token": token, **data}, follow_redirects=False
            )
            return response

        post(reason=NOTE, admin_password="wrong")
        assert status(world, market) == "open"
        r = post(reason=NOTE, admin_password=web.PASSWORD)
        assert r.status_code == 303
        assert status(world, market) == "locked"
        admin_page = c.get(page).text
        assert NOTE in admin_page and 'data-testid="lock-market"' not in admin_page
        assert f"Locked early by the admin: {NOTE}" in player_c.get(f"/markets/{market}").text
