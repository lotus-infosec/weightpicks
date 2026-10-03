"""Every page at phone and desktop size: no sideways scroll, finger-sized buttons, the
stylesheet applied, no console/CSP errors. Screenshots go to test-screens/."""

from pathlib import Path

import pytest
from playwright.sync_api import Browser, ConsoleMessage, Page, expect

from tests.e2e.conftest import RichServer

pytestmark = pytest.mark.e2e

SCREENS = Path("test-screens")  # git-ignored; pytest-playwright wipes test-results/
SIZES = {"phone": (375, 812), "desktop": (1280, 800)}
UNSTYLED = {"rgba(0, 0, 0, 0)", "rgb(255, 255, 255)"}  # browser defaults: no stylesheet


def check(page: Page, name: str, size: str, errors: list[str]) -> None:
    page.wait_for_load_state("networkidle")
    width = page.evaluate("document.documentElement.scrollWidth")
    viewport = page.evaluate("window.innerWidth")
    assert width <= viewport, f"{name}@{size}: page is {width}px wide in a {viewport}px window"
    bg = page.evaluate("getComputedStyle(document.documentElement).backgroundColor")
    assert bg not in UNSTYLED, f"{name}@{size}: stylesheet not applied (background {bg})"
    for box in page.locator("button.side:visible, button.primary:visible").all():
        height = box.bounding_box()["height"]  # type: ignore[index]
        assert height >= 44, f"{name}@{size}: a button is only {height}px tall"
    SCREENS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=SCREENS / f"{size}-{name}.png", full_page=True)
    assert errors == [], f"{name}@{size}: {errors}"


@pytest.mark.parametrize("size", SIZES)
def test_pages_fit_and_render(browser: Browser, rich_server: RichServer, size: str) -> None:
    w, h = SIZES[size]
    context = browser.new_context(viewport={"width": w, "height": h}, is_mobile=size == "phone")
    page = context.new_page()
    errors: list[str] = []
    expected_4xx = "the server responded with a status of 4"  # e.g. the wrong-password 400

    def on_console(m: ConsoleMessage) -> None:
        if m.type == "error" and expected_4xx not in m.text:
            errors.append(m.text)

    page.on("console", on_console)
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    url = rich_server.url

    page.goto(f"{url}/register")
    check(page, "register", size, errors)
    page.goto(f"{url}/login")
    page.get_by_label("Email").fill(rich_server.email)
    page.get_by_label("Password").fill("wrong password!")
    page.get_by_role("button", name="Log in").click()
    expect(page.get_by_role("alert")).to_be_visible()
    check(page, "login-error", size, errors)
    page.get_by_label("Password").fill(rich_server.password)
    page.get_by_role("button", name="Log in").click()
    expect(page).to_have_url(f"{url}/")

    check(page, "board-daily", size, errors)
    page.locator('article[data-metric="weight"] button.side-over').click()
    page.get_by_test_id("stake").fill("25")
    expect(page.get_by_test_id("slip")).to_be_in_viewport()
    check(page, "board-slip", size, errors)
    page.get_by_role("button", name="Close slip").click()

    page.get_by_role("tab", name="Weekly").click()
    expect(page).to_have_url(f"{url}/?tab=weekly")
    expect(page.get_by_role("tab", name="Weekly")).to_have_attribute("aria-selected", "true")
    expect(page.get_by_role("tab", name="Daily")).to_have_attribute("aria-selected", "false")
    expect(page.locator("article.market").first).to_be_visible()
    check(page, "board-weekly", size, errors)

    page.locator("a.market-title").first.click()
    expect(page.locator("h1")).to_be_visible()
    check(page, "market", size, errors)
    page.get_by_role("link", name="My bets").click()
    expect(page.locator("li.bet").first).to_be_visible()
    check(page, "my-bets", size, errors)
    page.get_by_role("link", name="Feed").click()
    check(page, "feed", size, errors)
    context.close()
