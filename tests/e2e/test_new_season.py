"""Issue #85, clicked through in a browser: with markets and a special event still open,
the admin freezes, ticks "Void and refund everything still open" and starts a new season;
it starts, nothing is left open, and players' stakes come back."""

import re

import pytest
from playwright.sync_api import Browser, expect

from tests.e2e.conftest import RichServer
from tests.e2e.test_layout import check
from tests.e2e.test_system_appearance import log_in

pytestmark = pytest.mark.e2e


def test_start_a_new_season_voiding_whats_open(browser: Browser, rich_server: RichServer) -> None:
    url, pw = rich_server.url, rich_server.password
    errors: list[str] = []
    context = browser.new_context(viewport={"width": 375, "height": 812}, is_mobile=True)
    page = context.new_page()
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    log_in(page, url, "admin@example.invalid", pw)
    page.goto(f"{url}/admin/season")
    freeze = page.locator('form[action="/admin/season/freeze"]')
    freeze.get_by_label("Your admin password").fill(pw)
    freeze.get_by_test_id("freeze-toggle").click()
    expect(page.get_by_test_id("season-unsettled")).to_contain_text("Still open or locked:")

    card = page.get_by_test_id("new-season")
    card.get_by_label("Goal weight").fill("205")
    card.get_by_label("Your admin password").fill(pw)
    card.get_by_role("button", name="Start season").click()  # not ticked: refused
    expect(page.get_by_role("alert")).to_contain_text(re.compile(r"still open or locked"))

    card = page.get_by_test_id("new-season")
    card.get_by_label("Goal weight").fill("205")
    card.get_by_test_id("void-open").check()
    card.get_by_label("Your admin password").fill(pw)
    card.get_by_role("button", name="Start season").click()
    expect(page.get_by_role("status")).to_contain_text("season")
    expect(page.get_by_test_id("season-current")).to_contain_text("Season 2")
    expect(page.get_by_test_id("frozen-banner")).to_have_count(0)  # betting reopened
    expect(page.get_by_test_id("season-history")).to_contain_text("ended")
    check(page, "admin-season-new", "phone", errors)
    page.goto(f"{url}/admin/markets?status=open")
    expect(page.locator("table tbody tr")).to_have_count(0)
    page.goto(f"{url}/admin/events")
    expect(page.get_by_test_id("pools")).to_contain_text("refunded")  # the special event
    context.close()

    # The player's open bets were voided and refunded, with the reason.
    context = browser.new_context(viewport={"width": 375, "height": 812}, is_mobile=True)
    page = context.new_page()
    log_in(page, url, rich_server.email, pw)
    page.goto(f"{url}/bets/mine?season=1")
    expect(page.get_by_test_id("bet-reason").first).to_contain_text("a new season started")
    context.close()
    assert errors == []
