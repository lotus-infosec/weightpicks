"""Build a 3-leg parlay from the bet sheet, switch a leg, see the combined odds, place
it; the Props tab shows yes/no markets. Screenshots go to test-screens/ for review."""

import re

import pytest
from playwright.sync_api import Browser, ConsoleMessage, expect

from tests.e2e.conftest import RichServer
from tests.e2e.test_layout import check

pytestmark = pytest.mark.e2e


@pytest.mark.parametrize("size", ["phone", "desktop"])
def test_build_and_place_a_parlay(browser: Browser, rich_server: RichServer, size: str) -> None:
    w, h = (375, 812) if size == "phone" else (1280, 800)
    context = browser.new_context(viewport={"width": w, "height": h}, is_mobile=size == "phone")
    page = context.new_page()
    errors: list[str] = []

    def on_console(m: ConsoleMessage) -> None:
        if m.type == "error":
            errors.append(m.text)

    page.on("console", on_console)
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    url = rich_server.url
    page.goto(f"{url}/login")
    page.get_by_label("Email").fill(rich_server.email)
    page.get_by_label("Password").fill(rich_server.password)
    page.get_by_role("button", name="Log in").click()
    expect(page).to_have_url(f"{url}/")

    sheet = page.get_by_test_id("slip")
    bar = page.get_by_test_id("parlay-bar")

    def leg(metric: str, side: int) -> None:
        """Open the market row, pick side 0 (over/yes) or 1 (under/no), add it."""
        page.locator(f'article[data-metric="{metric}"] button.mrow').first.click()
        sheet.locator("button.toggle").nth(side).click()
        page.get_by_test_id("add-leg").click()

    # Leg 1: tonight's daily weight market; legs 2 and 3: weekly steps and calories.
    leg("weight", 0)
    expect(bar).to_be_visible()
    page.get_by_role("tab", name="Weekly").click()
    expect(page.get_by_role("tab", name="Weekly")).to_have_attribute("aria-selected", "true")
    leg("steps", 0)
    leg("kcal", 1)
    expect(bar.locator(".parlay-count")).to_have_text("3")
    # The weekly weight market shares the start weigh-in with leg 1: refused in the sheet.
    leg("weight", 0)
    expect(page.get_by_test_id("slip-message")).to_contain_text("same weigh-in or day")
    page.keyboard.press("Escape")
    expect(bar.locator(".parlay-count")).to_have_text("3")

    # Issue #37: the other side of a leg's market switches that leg instead of refusing.
    steps = page.locator('article[data-metric="steps"] button.mrow').first
    steps.click()
    expect(sheet.locator("button.toggle").nth(0)).to_have_attribute("aria-pressed", "true")
    expect(page.get_by_test_id("add-leg")).to_have_text("Remove from parlay")
    sheet.locator("button.toggle").nth(1).click()
    expect(page.get_by_test_id("add-leg")).to_have_text("Switch parlay leg")
    page.get_by_test_id("add-leg").click()
    bar.click()
    legs = page.get_by_test_id("slip-legs").locator("li")
    expect(legs).to_have_count(3)
    expect(legs.nth(1)).to_contain_text("Under")
    page.keyboard.press("Escape")
    # Tapping the picked side again takes the leg out; adding it once more puts it back.
    steps.click()
    page.get_by_test_id("add-leg").click()
    expect(bar.locator(".parlay-count")).to_have_text("2")
    leg("steps", 1)
    expect(bar.locator(".parlay-count")).to_have_text("3")

    bar.click()
    page.get_by_test_id("parlay-stake").fill("10")
    expect(page.get_by_test_id("parlay-odds")).to_have_text(re.compile(r"^\+\d+$"))
    expect(page.get_by_test_id("parlay-payout")).to_have_text(re.compile(r"^\$\d+\.\d\d$"))
    check(page, "parlay-slip", size, errors)
    page.get_by_test_id("place-parlay").click()
    expect(page.get_by_test_id("bet-done")).to_contain_text("Parlay placed")
    expect(bar).to_be_hidden()

    page.get_by_role("link", name="My bets").click()
    first = page.locator("li.bet").first
    expect(first).to_contain_text("3-leg parlay")
    expect(first.locator("ol.legs li")).to_have_count(3)
    check(page, "my-bets-parlay", size, errors)

    page.goto(f"{url}/?tab=prop")
    expect(page.get_by_role("tab", name="Props")).to_have_attribute("aria-selected", "true")
    card = page.locator("article.market").first
    expect(card).to_contain_text("Yes/No")
    card.locator("button.mrow").click()
    expect(page.get_by_test_id("slip").locator("button.toggle-yes")).to_contain_text("Yes")
    expect(page.get_by_test_id("slip").locator("button.toggle-no")).to_contain_text("No")
    check(page, "board-props", size, errors)
    context.close()
