"""Issue #43, mobile-first board: compact market rows, a bottom sheet to pick a side and
stake, a parlay bar above the bottom navigation, and the bet-placed moment. Phone and
desktop, plus a second palette; viewport screenshots go to test-screens/ for review."""

import re

import pytest
from playwright.sync_api import Browser, Page, expect

from tests.e2e.conftest import RichServer
from tests.e2e.test_layout import SCREENS, compress
from tests.e2e.test_system_appearance import log_in

pytestmark = pytest.mark.e2e


def shot(page: Page, name: str) -> None:
    SCREENS.mkdir(parents=True, exist_ok=True)
    page.wait_for_timeout(400)  # let the sheet and check-mark animations finish
    page.screenshot(path=SCREENS / f"{name}.png")
    compress(SCREENS / f"{name}.png")


@pytest.mark.parametrize(
    ("size", "palette"), [("phone", "ember"), ("desktop", "ember"), ("phone", "lagoon")]
)
def test_board_sheet_parlay_and_placed(
    browser: Browser, rich_server: RichServer, size: str, palette: str
) -> None:
    w, h = (375, 812) if size == "phone" else (1280, 800)
    tag = f"{size}-{palette}"
    errors: list[str] = []
    if palette != "ember":
        admin = browser.new_context()
        page = admin.new_page()
        log_in(page, rich_server.url, "admin@example.invalid", rich_server.password)
        page.goto(f"{rich_server.url}/admin/appearance")
        page.locator(f'input[name="palette"][value="{palette}"]').check()
        page.get_by_role("button", name=re.compile("^Save")).first.click()
        admin.close()

    context = browser.new_context(viewport={"width": w, "height": h}, is_mobile=size == "phone")
    page = context.new_page()
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    log_in(page, rich_server.url, rich_server.email, rich_server.password)
    expect(page.get_by_test_id("dock-nav")).to_be_visible()
    shot(page, f"redesign-{tag}-board")

    # Tap a market: the sheet opens with both sides; pick, quick stake, see the return.
    weight = page.locator('article[data-metric="weight"] button.mrow').first
    weight.click()
    sheet = page.get_by_test_id("slip")
    expect(sheet).to_be_visible()
    sheet.locator("button.toggle").first.click()
    expect(sheet.locator("button.toggle").first).to_have_attribute("aria-pressed", "true")
    sheet.get_by_role("button", name="$10", exact=True).click()
    expect(page.get_by_test_id("payout")).to_have_text(re.compile(r"^\$\d+\.\d\d$"))
    shot(page, f"redesign-{tag}-sheet")

    # Build a parlay: three legs from different markets; the bar shows above the nav.
    page.get_by_test_id("add-leg").click()
    expect(page.get_by_test_id("parlay-bar")).to_be_visible()
    for metric in ("steps", "kcal"):
        page.locator(f'article[data-metric="{metric}"] button.mrow').last.click()
        sheet.locator("button.toggle").last.click()
        page.get_by_test_id("add-leg").click()
    expect(page.get_by_test_id("parlay-bar")).to_contain_text("3")
    shot(page, f"redesign-{tag}-parlay-bar")
    page.get_by_test_id("parlay-bar").click()
    expect(page.get_by_test_id("slip-legs").locator("li")).to_have_count(3)
    page.get_by_test_id("parlay-sheet").get_by_role("button", name="$10", exact=True).click()
    shot(page, f"redesign-{tag}-parlay-sheet")

    page.get_by_test_id("place-parlay").click()
    expect(page.get_by_test_id("bet-done")).to_contain_text("Parlay placed")
    shot(page, f"redesign-{tag}-placed")
    assert errors == []
    context.close()
