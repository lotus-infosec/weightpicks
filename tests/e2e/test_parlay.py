"""Build a 3-leg parlay on a phone, see the combined odds, place it; the Props
tab shows yes/no markets. Screenshots go to test-screens/ for review."""

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

    # Leg 1: tonight's daily weight market, then switch the slip to a parlay.
    page.locator('article[data-metric="weight"] button.side-over').first.click()
    page.get_by_test_id("parlay-toggle").check()
    expect(page.get_by_test_id("slip-legs").locator("li")).to_have_count(1)

    # Legs 2 and 3: weekly steps and weekly calories (different data, so allowed).
    page.get_by_role("tab", name="Weekly").click()
    expect(page.get_by_role("tab", name="Weekly")).to_have_attribute("aria-selected", "true")
    page.locator('article[data-metric="steps"] button.side-over').first.click()
    page.locator('article[data-metric="kcal"] button.side-under').first.click()
    expect(page.get_by_test_id("slip-legs").locator("li")).to_have_count(3)
    # The weekly weight market shares the start weigh-in with leg 1: refused at the slip.
    page.locator('article[data-metric="weight"] button.side-over').first.click()
    expect(page.get_by_test_id("slip-message")).to_contain_text("same weigh-in or day")
    expect(page.get_by_test_id("slip-legs").locator("li")).to_have_count(3)

    # Issue #37: the other side of a leg's market switches that leg instead of refusing.
    steps = page.locator('article[data-metric="steps"]').first
    legs = page.get_by_test_id("slip-legs").locator("li")
    expect(steps.locator("button.side-over")).to_have_attribute("aria-pressed", "true")
    steps.locator("button.side-under").click()
    expect(legs).to_have_count(3)
    expect(legs.nth(1)).to_contain_text("Under")
    expect(page.get_by_test_id("slip-message")).to_be_hidden()
    expect(steps.locator("button.side-under")).to_have_attribute("aria-pressed", "true")
    expect(steps.locator("button.side-over")).to_have_attribute("aria-pressed", "false")
    # Tapping the picked side again takes the leg out; tapping it once more puts it back.
    steps.locator("button.side-under").click()
    expect(legs).to_have_count(2)
    steps.locator("button.side-under").click()
    expect(legs).to_have_count(3)

    page.get_by_test_id("stake").fill("10")
    expect(page.get_by_test_id("parlay-odds")).to_contain_text(
        re.compile(r"3 legs · combined \+\d+")
    )
    expect(page.get_by_test_id("payout")).to_have_text(re.compile(r"^\$\d+\.\d\d$"))
    check(page, "parlay-slip", size, errors)
    page.get_by_test_id("place").click()
    expect(page.get_by_test_id("slip-message")).to_contain_text("Parlay placed")

    page.get_by_role("link", name="My bets").click()
    first = page.locator("li.bet").first
    expect(first).to_contain_text("3-leg parlay")
    expect(first.locator("ol.legs li")).to_have_count(3)
    check(page, "my-bets-parlay", size, errors)

    page.goto(f"{url}/?tab=prop")
    expect(page.get_by_role("tab", name="Props")).to_have_attribute("aria-selected", "true")
    card = page.locator("article.market").first
    expect(card.locator("button.side-yes")).to_contain_text("Yes")
    expect(card.locator("button.side-no")).to_contain_text("No")
    check(page, "board-props", size, errors)
    context.close()
