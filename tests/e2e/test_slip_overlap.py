"""Issue #36: with a parlay slip open, the last market on the board can still be scrolled
above the slip and tapped; the slip can be minimised to a one-line bar."""

import pytest
from playwright.sync_api import Browser, expect

from tests.e2e.conftest import RichServer
from tests.e2e.test_layout import check

pytestmark = pytest.mark.e2e


def test_slip_never_covers_the_last_market(browser: Browser, rich_server: RichServer) -> None:
    context = browser.new_context(viewport={"width": 375, "height": 812}, is_mobile=True)
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    url = rich_server.url
    page.goto(f"{url}/login")
    page.get_by_label("Email").fill(rich_server.email)
    page.get_by_label("Password").fill(rich_server.password)
    page.get_by_role("button", name="Log in").click()
    expect(page).to_have_url(f"{url}/")

    page.get_by_role("tab", name="Weekly").click()
    expect(page.get_by_role("tab", name="Weekly")).to_have_attribute("aria-selected", "true")
    page.locator('article[data-metric="steps"] button.side-over').first.click()
    page.get_by_test_id("parlay-toggle").check()
    page.locator('article[data-metric="kcal"] button.side-under').first.click()
    page.locator('article[data-metric="active_minutes"] button.side-over').first.click()
    slip = page.get_by_test_id("slip")
    expect(page.get_by_test_id("slip-legs").locator("li")).to_have_count(3)

    page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
    last = page.locator("article.market").last
    last_bottom = last.bounding_box()["y"] + last.bounding_box()["height"]  # type: ignore[index]
    slip_top = slip.bounding_box()["y"]  # type: ignore[index]
    assert last_bottom <= slip_top, (last_bottom, slip_top)
    last.locator("button.side").last.click(trial=True)  # not covered: it would receive the tap
    box = slip.bounding_box()
    assert box is not None and box["height"] <= 0.7 * 812 + 1
    check(page, "slip-open-bottom", "phone", errors)

    page.get_by_test_id("slip-minimise").click()
    expect(page.get_by_test_id("slip-summary")).to_contain_text("3 legs")
    expect(page.get_by_test_id("stake")).to_be_hidden()
    minimised = slip.bounding_box()
    assert minimised is not None and minimised["height"] < 90
    check(page, "slip-minimised", "phone", errors)
    page.get_by_test_id("slip-minimise").click()
    expect(page.get_by_test_id("stake")).to_be_visible()
    context.close()
