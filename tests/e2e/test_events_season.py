"""STAGE14: buy into a special event and change the guess on a phone and a desktop; the
admin event preview and Season page; the frozen banner. Screenshots go to test-screens/."""

import pytest
from playwright.sync_api import Browser, ConsoleMessage, Page, expect

from tests.e2e.conftest import RichServer
from tests.e2e.test_layout import check

pytestmark = pytest.mark.e2e


def log_in(page: Page, url: str, email: str, password: str) -> None:
    page.goto(f"{url}/login")
    page.get_by_label("Email").fill(email)
    page.get_by_label("Password").fill(password)
    page.get_by_role("button", name="Log in").click()


@pytest.mark.parametrize("size", ["phone", "desktop"])
def test_events_and_season(browser: Browser, rich_server: RichServer, size: str) -> None:
    w, h = (375, 812) if size == "phone" else (1280, 800)
    url, pw = rich_server.url, rich_server.password
    errors: list[str] = []

    def on_console(m: ConsoleMessage) -> None:
        if m.type == "error" and "status of 4" not in m.text:
            errors.append(m.text)

    context = browser.new_context(viewport={"width": w, "height": h}, is_mobile=size == "phone")
    page = context.new_page()
    page.on("console", on_console)
    page.on("pageerror", lambda exc: errors.append(str(exc)))

    # Player: buy in, then change the guess; Alex's guess stays hidden while open.
    log_in(page, url, rich_server.email, pw)
    page.goto(f"{url}/?tab=events")
    expect(page.get_by_role("tab", name="Events")).to_have_attribute("aria-selected", "true")
    pot = page.locator("article.pool").first
    expect(pot.get_by_test_id("enter")).to_have_text("Buy in for $25.00")
    pot.get_by_test_id("guess").fill("219.6")
    pot.get_by_test_id("enter").click()
    expect(page.get_by_test_id("pool-message")).to_contain_text("Good luck")
    expect(page.get_by_test_id("balance")).to_have_text("$915.00")  # header updated too
    expect(page.locator("article.pool").first.get_by_test_id("pot")).to_have_text("$50.00")
    page.locator("article.pool").first.get_by_test_id("guess").fill("219.1")
    page.locator("article.pool").first.get_by_test_id("enter").click()
    expect(page.get_by_test_id("pool-message")).to_contain_text("Guess updated")
    expect(page.locator("article.pool").first).not_to_contain_text("218.5")  # Alex's guess
    check(page, "board-events", size, errors)
    context.close()

    # Admin: a manual event preview, then the Season page; freeze it.
    context = browser.new_context(viewport={"width": w, "height": h}, is_mobile=size == "phone")
    page = context.new_page()
    page.on("console", on_console)
    log_in(page, url, "admin@example.invalid", pw)
    page.goto(f"{url}/admin/events")
    form = page.get_by_test_id("event-form")
    form.get_by_label("Title").fill("Halloween pot")
    form.get_by_label("Question").fill("What will the scale say on Halloween morning?")
    form.get_by_label("Target date").fill("2026-10-31")
    form.get_by_label("Buy-in ($)").fill("40")
    form.get_by_role("button", name="Preview").click()
    preview = page.get_by_test_id("event-preview")
    expect(preview).to_contain_text("Halloween pot")
    expect(preview).to_contain_text("$40.00")
    expect(page.get_by_test_id("pools")).to_contain_text("Columbus Day pot")
    check(page, "admin-event-preview", size, errors)

    page.goto(f"{url}/admin/season")
    expect(page.get_by_test_id("season-current")).to_contain_text("Season 1")
    check(page, "admin-season", size, errors)
    freeze = page.locator('form[action="/admin/season/freeze"]')
    freeze.get_by_label("Your admin password").fill(pw)
    freeze.get_by_test_id("freeze-toggle").click()
    expect(page.get_by_role("status")).to_contain_text("Instance frozen")
    expect(page.get_by_test_id("new-season")).to_be_visible()
    context.close()

    # Player again: the frozen banner, and the Events form refuses.
    context = browser.new_context(viewport={"width": w, "height": h}, is_mobile=size == "phone")
    page = context.new_page()
    page.on("console", on_console)
    log_in(page, url, rich_server.email, pw)
    page.goto(f"{url}/?tab=events")
    expect(page.get_by_test_id("frozen-banner")).to_contain_text("Betting is paused")
    expect(page.locator("article.pool").first).to_contain_text("Entries are paused")
    expect(page.get_by_test_id("guess")).to_have_count(0)
    check(page, "board-frozen", size, errors)
    context.close()
