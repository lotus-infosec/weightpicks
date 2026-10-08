"""The AI review queue, AI runs and notes pages, and an AI prop's blurb on the
board, on a phone and a desktop. Screenshots go to test-screens/ for review."""

import pytest
from playwright.sync_api import Browser, ConsoleMessage, Page, expect

from tests.e2e.conftest import RichServer
from tests.e2e.test_layout import check

pytestmark = pytest.mark.e2e

BLURB = "Last week set a high bar for steps. Can this week clear it?"


def log_in(page: Page, url: str, email: str, password: str) -> None:
    page.goto(f"{url}/login")
    page.get_by_label("Email").fill(email)
    page.get_by_label("Password").fill(password)
    page.get_by_role("button", name="Log in").click()


@pytest.mark.parametrize("size", ["phone", "desktop"])
def test_ai_admin_pages_and_blurb(browser: Browser, rich_server: RichServer, size: str) -> None:
    w, h = (375, 812) if size == "phone" else (1280, 800)
    url = rich_server.url
    errors: list[str] = []

    def on_console(m: ConsoleMessage) -> None:
        if m.type == "error":
            errors.append(m.text)

    context = browser.new_context(viewport={"width": w, "height": h}, is_mobile=size == "phone")
    page = context.new_page()
    page.on("console", on_console)
    page.on("pageerror", lambda exc: errors.append(str(exc)))

    log_in(page, url, "admin@example.invalid", rich_server.password)
    page.goto(f"{url}/admin/props")
    queue = page.get_by_test_id("ai-queue")
    expect(queue.get_by_test_id("proposal")).to_have_count(1)
    expect(queue).to_contain_text("A big calorie week would make this one interesting.")
    expect(queue).to_contain_text("fair chance")
    check(page, "admin-ai-queue", size, errors)

    page.goto(f"{url}/admin/ai")
    expect(page.get_by_test_id("ai-usage")).to_contain_text("39 of 5,000 neurons")
    expect(page.get_by_test_id("ai-runs")).to_contain_text("link or markup")
    check(page, "admin-ai-runs", size, errors)

    page.goto(f"{url}/admin/notes")
    expect(page.get_by_test_id("notes")).to_contain_text("Traveling Thu-Sun")
    check(page, "admin-notes", size, errors)

    # Approve the waiting proposal: it becomes a market and the queue empties.
    page.goto(f"{url}/admin/props")
    page.get_by_test_id("approve").click()
    expect(page.get_by_role("status")).to_contain_text("AI prop approved")
    expect(page.get_by_test_id("ai-queue-empty")).to_be_visible()
    context.close()

    context = browser.new_context(viewport={"width": w, "height": h}, is_mobile=size == "phone")
    page = context.new_page()
    page.on("console", on_console)
    log_in(page, url, rich_server.email, rich_server.password)
    page.goto(f"{url}/?tab=prop")
    card = page.locator("article.market", has=page.get_by_text(BLURB))
    expect(card.get_by_test_id("blurb")).to_have_text(BLURB)
    expect(card.locator("button.side-yes")).to_contain_text("Yes")
    check(page, "board-ai-prop", size, errors)
    context.close()
