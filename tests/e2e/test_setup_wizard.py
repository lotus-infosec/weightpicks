"""STAGE08: a fresh instance sets itself up through /setup at phone size."""

import pytest
from playwright.sync_api import Browser, ConsoleMessage, Page, expect

from tests.e2e.conftest import FreshServer
from tests.e2e.test_layout import check

pytestmark = pytest.mark.e2e
PASSWORD = "correct horse battery"


def test_setup_wizard_end_to_end(browser: Browser, fresh_server: FreshServer) -> None:
    context = browser.new_context(viewport={"width": 375, "height": 812}, is_mobile=True)
    page: Page = context.new_page()
    errors: list[str] = []

    def on_console(m: ConsoleMessage) -> None:
        if m.type == "error" and "the server responded with a status of 4" not in m.text:
            errors.append(m.text)

    page.on("console", on_console)
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    url = fresh_server.url

    page.goto(f"{url}/")  # everything leads to setup on a fresh instance
    expect(page).to_have_url(f"{url}/setup")
    check(page, "setup-00-token", "phone", errors)
    page.get_by_label("Setup token").fill(fresh_server.token)
    page.get_by_role("button", name="Start setup").click()
    expect(page).to_have_url(f"{url}/setup/step/1")

    def save(n: int) -> None:
        check(page, f"setup-{n:02d}", "phone", errors)
        page.get_by_role("button", name="Save and continue").click()
        expect(page).to_have_url(f"{url}/setup/step/{n + 1}")

    page.get_by_label("Email").fill("admin@example.invalid")
    page.get_by_label("Display name").fill("Rho")
    page.get_by_label("Password", exact=False).first.fill(PASSWORD)
    page.get_by_label("Repeat password").fill(PASSWORD)
    save(1)
    page.get_by_label("Name shown to players").fill("Rho")
    page.get_by_label("Starting weight").fill("212.4")
    page.get_by_label("Goal weight").fill("185")
    save(2)

    page.reload()  # a refresh mid-wizard loses nothing
    page.locator('ol.steps a[href="/setup/step/2"]').click()
    expect(page.get_by_label("Starting weight")).to_have_value("212.4")
    page.get_by_role("button", name="Save and continue").click()

    page.get_by_label("Bets lock at").fill("21:30")
    save(3)
    save(4)  # economy defaults
    page.get_by_label("Workouts").uncheck()
    save(5)
    save(6)  # Garmin is informational
    save(7)  # skip Workers AI
    page.get_by_label("Bets placed").fill("https://discord.com/api/webhooks/123/abcDEF")
    save(8)
    save(9)  # skip SMTP
    code = page.get_by_test_id("registration-code").inner_text()
    assert len(code) == 14
    save(10)
    page.get_by_label("App name").fill("Rho Picks")
    page.get_by_label("Grove").check()
    save(11)

    expect(page.locator("dl.review")).to_contain_text("Rho Picks")
    expect(page.locator("dl.review")).to_contain_text("Discord webhooks 1")
    check(page, "setup-12-review", "phone", errors)
    page.get_by_role("button", name="Finish setup").click()

    expect(page).to_have_url(f"{url}/login?welcome=1")
    expect(page.get_by_role("status")).to_contain_text("Setup is complete")
    check(page, "setup-13-login", "phone", errors)
    page.get_by_label("Email").fill("admin@example.invalid")
    page.get_by_label("Password").fill(PASSWORD)
    page.get_by_role("button", name="Log in").click()
    expect(page).to_have_url(f"{url}/admin")
    expect(page.locator("header .brand")).to_have_text("Rho Picks")
    accent = page.evaluate(
        "getComputedStyle(document.documentElement).getPropertyValue('--color-accent').trim()"
    )
    assert accent == "#8fd16a"  # grove
    response = page.goto(f"{url}/setup")
    assert response is not None and response.status == 404
    context.close()
