"""Issue #42: another player's open bet shows who and which market, never the side, line,
odds or stake, in the feed and on a market's page. Your own bets, settled bets and the
admin's view stay complete."""

from datetime import date

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.clock import SystemClock
from app.models import User
from app.services import auth, settlement
from app.web.main import create_app
from tests.integration import web
from tests.integration.test_bets_settlement import bet, change, to_settle_time, weight_market
from tests.integration.world import World

STAKE = 2_345  # $23.45: easy to spot in the HTML


def _user_id(w: World, n: int) -> int:
    with w.engine.connect() as conn:
        email = f"player{n}@example.invalid"
        return int(conn.execute(select(User.id).where(User.email == email)).scalar_one())


def _bets_html(c: TestClient, market_id: int) -> str:
    return str(c.get("/bets/feed").text) + str(c.get(f"/markets/{market_id}").text)


def test_open_bets_hide_side_and_stake_until_they_settle(world: World) -> None:
    code = auth.rotate_registration_code(world.engine, SystemClock())
    app = create_app(world.settings, domain_clock=world.clock)
    actual = change(date(2026, 10, 5), date(2026, 10, 6))
    market = weight_market(world, actual - 5)
    with web.client(app) as alex, web.client(app) as sam, web.client(app) as admin:
        web.register(alex, code, 1)
        web.register(sam, code, 2)
        web.as_admin(admin, world.engine)
        bet(world, _user_id(world, 1), market, "under", stake=STAKE)

        other = _bets_html(sam, market)
        assert "Player 1" in other and 'data-testid="bet-hidden"' in other
        for secret in ("$23.45", "to return"):
            assert secret not in other, secret
        own = alex.get("/bets/feed").text
        assert "$23.45" in own and "Under" in own  # your own bet: in full
        assert "$23.45" in admin.get(f"/admin/markets/{market}").text  # the admin sees all

        to_settle_time(world)
        assert settlement.settle_market(world.engine, world.clock, market).settled
        settled = _bets_html(sam, market)
        assert "$23.45" in settled and 'data-testid="bet-hidden"' not in settled
