"""Admin -> System (health, backup now), Appearance with an uploaded logo (the
top bar and favicon follow it), and the password-reset request page. Phone and desktop;
screenshots go to test-screens/ for review."""

from pathlib import Path

import pytest
from PIL import Image, ImageDraw
from playwright.sync_api import Browser, ConsoleMessage, Page, expect

from tests.e2e.conftest import RichServer
from tests.e2e.test_layout import check

pytestmark = pytest.mark.e2e


def log_in(page: Page, url: str, email: str, password: str) -> None:
    page.goto(f"{url}/login")
    page.get_by_label("Email").fill(email)
    page.get_by_label("Password").fill(password)
    page.get_by_role("button", name="Log in").click()


def a_logo(tmp_path: Path) -> Path:
    im = Image.new("RGBA", (400, 400), (0, 0, 0, 0))
    draw = ImageDraw.Draw(im)
    draw.rounded_rectangle((10, 10, 390, 390), radius=60, fill=(214, 120, 40, 255))
    draw.ellipse((120, 120, 280, 280), fill=(255, 245, 230, 255))
    path = tmp_path / "logo.png"
    im.save(path)
    return path


@pytest.mark.parametrize("size", ["phone", "desktop"])
def test_system_appearance_and_reset(
    browser: Browser, rich_server: RichServer, size: str, tmp_path: Path
) -> None:
    w, h = (375, 812) if size == "phone" else (1280, 800)
    url, pw = rich_server.url, rich_server.password
    errors: list[str] = []

    def on_console(m: ConsoleMessage) -> None:
        if m.type == "error":
            errors.append(m.text)

    context = browser.new_context(viewport={"width": w, "height": h}, is_mobile=size == "phone")
    page = context.new_page()
    page.on("console", on_console)
    page.on("pageerror", lambda exc: errors.append(str(exc)))

    # Logged out: the login page has the favicon and the password-reset link.
    page.goto(f"{url}/login")
    icon = page.locator('link[rel="icon"]').get_attribute("href")
    assert icon is not None and icon.startswith("/brand/favicon.png?v=")
    assert page.request.get(f"{url}{icon}").status == 200
    page.get_by_test_id("forgot").click()
    expect(page.get_by_role("heading", name="Reset your password")).to_be_visible()
    check(page, "reset-request", size, errors)

    log_in(page, url, "admin@example.invalid", pw)
    page.goto(f"{url}/admin/system")
    expect(page.get_by_test_id("health")).to_contain_text("Ledger check")
    page.get_by_test_id("backup-now").click()
    expect(page.get_by_role("status")).to_contain_text("Backup created")
    expect(page.get_by_test_id("backups")).to_contain_text("-manual-admin.tar.gz")
    check(page, "admin-system", size, errors)

    page.goto(f"{url}/admin/appearance")
    default = page.request.get(f"{url}/brand/mark.png").body()
    card = page.get_by_test_id("logo-card")
    card.get_by_test_id("logo-file").set_input_files(str(a_logo(tmp_path)))
    card.get_by_label("Your admin password").first.fill(pw)
    card.get_by_role("button", name="Upload logo").click()
    expect(page.get_by_role("status")).to_contain_text("Logo saved")
    expect(page.get_by_text("Your uploaded logo.")).to_be_visible()
    assert page.request.get(f"{url}/brand/mark.png").body() != default
    mark = page.locator("img.brand-mark").get_attribute("src")
    assert mark is not None and "?v=default" not in mark  # the top bar asks for the new one
    check(page, "admin-appearance", size, errors)
    context.close()
