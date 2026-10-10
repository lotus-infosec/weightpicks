"""Issue #36 with the redesign: while a parlay is building, its bar sits above the bottom
navigation and the page pads for both, so the last market can always be scrolled into
view and tapped; the bet sheet is capped below the full screen height."""

import pytest
from playwright.sync_api import Browser, expect

from tests.e2e.conftest import RichServer
from tests.e2e.test_layout import check

pytestmark = pytest.mark.e2e


def test_parlay_bar_never_covers_the_last_market(browser: Browser, rich_server: RichServer) -> None:
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

    sheet = page.get_by_test_id("slip")
    page.get_by_role("tab", name="Weekly").click()
    expect(page.get_by_role("tab", name="Weekly")).to_have_attribute("aria-selected", "true")
    for metric in ("steps", "kcal", "active_minutes"):
        page.locator(f'article[data-metric="{metric}"] button.mrow').first.click()
        expect(sheet).to_be_visible()
        box = sheet.bounding_box()
        assert box is not None and box["height"] <= 0.88 * 812 + 1
        sheet.locator("button.toggle").first.click()
        page.get_by_test_id("add-leg").click()
    bar = page.get_by_test_id("parlay-bar")
    expect(bar.locator(".parlay-count")).to_have_text("3")

    page.wait_for_load_state("networkidle")
    page.mouse.wheel(0, 5000)  # scroll to the very bottom, as a thumb would
    page.wait_for_function(
        "() => window.scrollY + window.innerHeight >= document.documentElement.scrollHeight - 1"
    )
    last = page.locator("article.market").last
    last_box, bar_box = last.bounding_box(), bar.bounding_box()
    assert last_box is not None and bar_box is not None
    assert last_box["y"] + last_box["height"] <= bar_box["y"], (last_box, bar_box)
    last.locator("button.mrow").click(trial=True)  # not covered: it would receive the tap
    check(page, "parlay-bar-bottom", "phone", errors)
    context.close()
