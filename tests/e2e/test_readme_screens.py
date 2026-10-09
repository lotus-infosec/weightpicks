"""Screenshots for the README, taken from the e2e instance (simulated data, fake players).

Run with WP_README_SCREENS=docs/assets/screens to refresh the committed images:

    WP_README_SCREENS=docs/assets/screens PLAYWRIGHT_WS_ENDPOINT=ws://127.0.0.1:3123/ \\
        uv run pytest -m e2e tests/e2e/test_readme_screens.py

Without it the shots go to test-screens/ like every other e2e screenshot."""

import os
import re
from pathlib import Path

import pytest
from PIL import Image
from playwright.sync_api import Browser, Page, expect

from tests.e2e.conftest import RichServer
from tests.e2e.test_layout import SCREENS
from tests.e2e.test_system_appearance import log_in

pytestmark = pytest.mark.e2e

OUT = Path(os.environ.get("WP_README_SCREENS") or SCREENS / "readme")


def shot(page: Page, name: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    page.wait_for_timeout(450)  # sheet and check-mark animations
    path = OUT / f"{name}.png"
    page.screenshot(path=path)
    with Image.open(path) as im:  # keep the repo small: 256-colour PNGs look the same here
        im.convert("RGB").quantize(256, method=Image.Quantize.MEDIANCUT).save(path, optimize=True)


def test_readme_screens(browser: Browser, rich_server: RichServer) -> None:
    url = rich_server.url
    phone = browser.new_context(
        viewport={"width": 390, "height": 844}, is_mobile=True, device_scale_factor=2
    )
    page = phone.new_page()
    log_in(page, url, rich_server.email, rich_server.password)
    expect(page.get_by_test_id("dock-nav")).to_be_visible()
    shot(page, "board")

    sheet = page.get_by_test_id("slip")
    page.locator('article[data-metric="weight"] button.mrow').first.click()
    sheet.locator("button.toggle").first.click()
    sheet.get_by_role("button", name="$25", exact=True).click()
    shot(page, "bet-sheet")

    page.get_by_test_id("add-leg").click()
    page.get_by_role("tab", name="Weekly").click()
    expect(page.get_by_role("tab", name="Weekly")).to_have_attribute("aria-selected", "true")
    for metric in ("steps", "kcal"):
        page.locator(f'article[data-metric="{metric}"] button.mrow').first.click()
        sheet.locator("button.toggle").last.click()
        page.get_by_test_id("add-leg").click()
    page.get_by_test_id("parlay-bar").click()
    page.get_by_test_id("parlay-stake").fill("10")
    expect(page.get_by_test_id("parlay-payout")).to_have_text(re.compile(r"^\$\d"))
    shot(page, "parlay")
    page.get_by_test_id("place-parlay").click()
    expect(page.get_by_test_id("bet-done")).to_be_visible()
    shot(page, "bet-placed")

    for name, link in (("my-bets", "My bets"), ("leaderboard", "Leaders"), ("stats", "Stats")):
        page.get_by_role("link", name=link).click()
        page.wait_for_load_state("networkidle")
        shot(page, name)
    phone.close()

    desktop = browser.new_context(viewport={"width": 1280, "height": 800})
    page = desktop.new_page()
    log_in(page, url, "admin@example.invalid", rich_server.password)
    expect(page).to_have_url(f"{url}/admin")
    page.goto(f"{url}/admin/markets")  # the dashboard would show this test instance's idle worker
    shot(page, "admin")
    desktop.close()
