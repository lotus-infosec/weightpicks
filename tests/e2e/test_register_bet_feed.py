"""On a phone-sized screen, register -> place a single -> see it in the feed."""

import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import LiveServer

pytestmark = pytest.mark.e2e


def test_register_bet_and_see_it_in_the_feed(page: Page, live_server: LiveServer) -> None:
    errors: list[str] = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda exc: errors.append(str(exc)))

    page.goto(f"{live_server.url}/register")
    page.get_by_label("Registration code").fill(live_server.code.lower())
    page.get_by_label("Email").fill("e2e@example.invalid")
    page.get_by_label("Display name").fill("E2E Player")
    page.get_by_label("Password").fill("correct horse battery")
    page.get_by_role("button", name="Create account").click()

    expect(page).to_have_url(f"{live_server.url}/")
    expect(page.get_by_test_id("balance")).to_have_text("$1,000.00")
    weight = page.locator('article[data-metric="weight"]')
    expect(weight).to_have_count(1)

    weight.locator("button.side-over").click()
    slip = page.get_by_test_id("slip")
    expect(slip).to_be_visible()
    page.get_by_test_id("stake").fill("25")
    expect(page.get_by_test_id("payout")).to_have_text(re.compile(r"^\$\d+\.\d\d$"))
    page.get_by_test_id("place").click()
    expect(page.get_by_test_id("slip-message")).to_contain_text("Bet placed: $25.00")

    page.get_by_role("link", name="Feed").click()
    expect(page).to_have_url(f"{live_server.url}/bets/feed")
    feed = page.locator("#feed")
    expect(feed).to_contain_text("E2E Player")
    expect(feed).to_contain_text("$25.00")
    expect(feed).not_to_contain_text("e2e@example.invalid")

    page.get_by_role("link", name="My bets").click()
    expect(page.locator("body")).to_contain_text("Balance $975.00")
    assert errors == [], errors  # includes any CSP violation
