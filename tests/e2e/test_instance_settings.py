"""Admin -> Settings -> Instance (issues #17, #30): public URL and the time zone change
warning. Only the preview is opened (the shared server keeps its zone). Phone and
desktop; screenshots go to test-screens/ for review."""

import pytest
from playwright.sync_api import Browser, ConsoleMessage, expect

from tests.e2e.conftest import RichServer
from tests.e2e.test_layout import check
from tests.e2e.test_system_appearance import log_in

pytestmark = pytest.mark.e2e


@pytest.mark.parametrize("size", ["phone", "desktop"])
def test_instance_settings(browser: Browser, rich_server: RichServer, size: str) -> None:
    w, h = (375, 812) if size == "phone" else (1280, 800)
    errors: list[str] = []

    def on_console(m: ConsoleMessage) -> None:
        if m.type == "error":
            errors.append(m.text)

    context = browser.new_context(viewport={"width": w, "height": h}, is_mobile=size == "phone")
    page = context.new_page()
    page.on("console", on_console)
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    log_in(page, rich_server.url, "admin@example.invalid", rich_server.password)

    page.goto(f"{rich_server.url}/admin/discord")
    card = page.get_by_test_id("instance-settings")
    expect(card).to_be_visible()
    expect(page.get_by_test_id("public-url-source")).to_be_visible()
    card.scroll_into_view_if_needed()
    check(page, "admin-instance-settings", size, errors)

    page.get_by_label("New time zone").fill("America/Chicago")
    page.get_by_role("button", name="Review change").click()
    warning = page.get_by_test_id("timezone-warning")
    expect(warning).to_be_visible()
    expect(warning).to_contain_text("Change to America/Chicago?")
    warning.scroll_into_view_if_needed()
    check(page, "admin-timezone-warning", size, errors)
    page.get_by_role("link", name="Cancel").click()
    expect(page.get_by_test_id("current-timezone")).to_have_text("America/New_York")
    context.close()
