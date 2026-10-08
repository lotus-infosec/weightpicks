"""A simulated week operated from the admin UI only (no SQL, no CLI)."""

import re

import pytest
from playwright.sync_api import Browser, ConsoleMessage, Page, expect

from tests.e2e.conftest import OpsServer
from tests.e2e.test_layout import check

pytestmark = pytest.mark.e2e


def advance(page: Page, url: str, days: int = 0, hours: int = 0) -> None:
    page.goto(f"{url}/dev/clock")
    page.locator('form[action="/dev/clock/advance"] input[name="days"]').fill(str(days))
    page.locator('form[action="/dev/clock/advance"] input[name="hours"]').fill(str(hours))
    page.locator('form[action="/dev/clock/advance"] button').click()
    expect(page).to_have_url(f"{url}/dev/clock")


@pytest.mark.parametrize("size", ["phone", "desktop"])
def test_operate_a_week_from_the_admin_ui(
    browser: Browser, ops_server: OpsServer, size: str
) -> None:
    width, height = (375, 812) if size == "phone" else (1280, 900)
    context = browser.new_context(
        viewport={"width": width, "height": height}, is_mobile=size == "phone"
    )
    page = context.new_page()
    errors: list[str] = []

    def on_console(m: ConsoleMessage) -> None:
        if m.type == "error" and "the server responded with a status of 4" not in m.text:
            errors.append(m.text)

    page.on("console", on_console)
    url, pw = ops_server.url, ops_server.password
    page.goto(f"{url}/login")
    page.get_by_label("Email").fill(ops_server.admin_email)
    page.get_by_label("Password").fill(pw)
    page.get_by_role("button", name="Log in").click()
    expect(page).to_have_url(f"{url}/admin")
    check(page, "admin-dashboard", size, errors)

    # Sync now queues a command for the worker.
    page.get_by_role("button", name="Sync now").click()
    expect(page.get_by_role("status")).to_contain_text("Sync requested")
    expect(page.get_by_test_id("sync-status")).to_contain_text("pending")

    # Day 1 -> 2: the weight market settles at the next weigh-in; the reckless player busts.
    advance(page, url, days=1, hours=3)
    page.goto(f"{url}/admin/users")
    check(page, "admin-users", size, errors)
    expect(page.locator("tr", has_text="Reckless")).to_contain_text("bust")

    # Void an open market (password re-prompt).
    page.goto(f"{url}/admin/markets?status=open")
    check(page, "admin-markets", size, errors)
    page.locator("table a").first.click()
    page.get_by_label("Reason").fill("bad data feed")
    page.get_by_label("Your admin password").fill("wrong password")
    page.get_by_role("button", name="Void market").click()
    expect(page.get_by_role("alert")).to_contain_text("not your password")
    page.get_by_label("Reason").fill("bad data feed")
    page.get_by_label("Your admin password").fill(pw)
    page.get_by_role("button", name="Void market").click()
    expect(page.get_by_role("status")).to_contain_text("Market voided")
    check(page, "admin-market-voided", size, errors)

    # Freeze, then unfreeze the steady player; adjust their balance.
    page.goto(f"{url}/admin/users/{ops_server.steady_id}")
    page.get_by_role("button", name="Freeze (no betting)").click()
    expect(page.get_by_role("status")).to_contain_text("Player frozen")
    page.get_by_role("button", name="Unfreeze").click()
    check(page, "admin-user", size, errors)
    page.goto(f"{url}/admin/bank")
    adjust = page.locator('form[action="/admin/bank/adjust"]')
    adjust.get_by_label("Player").select_option(str(ops_server.steady_id))
    adjust.get_by_label("Amount ($)").fill("-10")
    adjust.get_by_label("Reason").fill("duplicate grant")
    adjust.get_by_label("Your admin password").fill(pw)
    adjust.get_by_role("button", name="Adjust balance").click()
    expect(page.get_by_role("status")).to_contain_text("Balance adjusted")

    # The bailout opens two days after the bust.
    expect(page.locator("form.bailout")).to_contain_text("opens")
    advance(page, url, days=2)
    page.goto(f"{url}/admin/bank")
    bailout = page.locator("form.bailout")
    expect(bailout).to_contain_text("ready")
    bailout.get_by_label("Your admin password").fill(pw)
    bailout.get_by_role("button", name="Pay bailout").click()
    expect(page.get_by_role("status")).to_contain_text("Bailout paid")
    check(page, "admin-bank", size, errors)

    # Remove (ban) the reckless player.
    page.goto(f"{url}/admin/users/{ops_server.reckless_id}")
    page.get_by_text("Remove (ban) this player").click()
    ban = page.locator(f'form[action="/admin/users/{ops_server.reckless_id}/ban"]')
    ban.get_by_label("Reason").fill("testing removal")
    ban.get_by_label("Your admin password").fill(pw)
    ban.get_by_role("button", name="Remove player").click()
    expect(page.get_by_role("status")).to_contain_text("Player removed")

    # New registration code, then the audit log shows every action.
    page.goto(f"{url}/admin/registration")
    page.get_by_label("Your admin password").fill(pw)
    page.get_by_role("button", name="Make a new code").click()
    expect(page.get_by_test_id("registration-code")).to_have_text(
        re.compile(r"^\w{4}-\w{4}-\w{4}$")
    )
    check(page, "admin-registration", size, errors)
    page.goto(f"{url}/admin/discord")
    expect(page.get_by_test_id("hook-busts")).to_have_text("off")
    page.get_by_label("Props and futures").check()
    page.get_by_role("button", name="Save", exact=True).first.click()
    expect(page.get_by_role("status")).to_contain_text("Settings saved")
    check(page, "admin-discord", size, errors)
    page.get_by_role("link", name="Props").click()
    expect(page.get_by_test_id("prop-form")).to_be_visible()
    check(page, "admin-props", size, errors)
    page.goto(f"{url}/admin/audit")
    log = page.locator("table")
    for action in (
        "sync.request",
        "reauth_failed",
        "market.void",
        "user.freeze",
        "user.unfreeze",
        "bank.adjust",
        "bank.bailout",
        "user.ban",
        "registration.rotate",
        "settings.flag",
    ):
        expect(log).to_contain_text(action)
    check(page, "admin-audit", size, errors)
    context.close()
