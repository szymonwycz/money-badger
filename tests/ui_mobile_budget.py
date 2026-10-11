"""Isolated Playwright test for the mobile BUDGET editor.

Run manually (never against the production process/database):
    python tests/ui_mobile_budget.py
    SHOTS=/tmp/mb-mobile-budget-shots python tests/ui_mobile_budget.py

The script starts its own gunicorn on a free localhost port and creates BUDGET_DB
inside a temporary directory. CHROMIUM may override /usr/bin/chromium.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

from playwright.sync_api import Page, sync_playwright


REPO = Path(__file__).resolve().parents[1]
CHROMIUM = os.environ.get("CHROMIUM", "/usr/bin/chromium")
LONG_PARENT = "A deliberately long household planning category"
LONG_CATEGORY = "Children clothing and school equipment for the whole season"
VIEWPORT = {"width": 390, "height": 844}


def resolved_db_path(raw: str) -> Path:
    path = Path(raw).expanduser()
    return (path if path.is_absolute() else REPO / path).resolve()


def is_inside(path: Path, directory: Path) -> bool:
    try:
        path.relative_to(directory.resolve())
        return True
    except ValueError:
        return False


def assert_safe_database(db_path: Path) -> None:
    if is_inside(db_path.resolve(), REPO):
        raise RuntimeError(
            f"Refusing to start: BUDGET_DB points inside the repository: {db_path}"
        )


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def api(base: str, path: str, method: str = "GET", payload=None):
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        base + path,
        data=body,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)


def wait_until(label: str, predicate, timeout: float = 8.0):
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            value = predicate()
            if value:
                return value
        except (OSError, urllib.error.URLError, AssertionError) as exc:
            last_error = exc
        time.sleep(0.1)
    raise AssertionError(f"Timed out waiting for {label}: {last_error or 'condition false'}")


def seed(base: str) -> tuple[int, int, int, int]:
    status, parent = api(base, "/api/categories", "POST", {"name": LONG_PARENT})
    assert status == 200 and parent["ok"]
    status, child = api(
        base,
        "/api/categories",
        "POST",
        {"name": LONG_CATEGORY, "parent_id": parent["id"]},
    )
    assert status == 200 and child["ok"]
    api(base, "/api/accounts", "POST", {"name": "Test current account", "type": "bank"})

    today = date.today()
    status, basic = api(
        base,
        "/api/basic",
        "POST",
        {
            "category_id": child["id"],
            "amount": 123456.78,
            "month": today.month,
            "year": today.year,
        },
    )
    assert status == 200 and basic["ok"]
    status, tx = api(
        base,
        "/api/transactions",
        "POST",
        {
            "date": today.isoformat(),
            "amount": 654321.09,
            "account": "Test current account",
            "description": "Long-name and six-digit layout sentinel",
            "category_id": child["id"],
        },
    )
    assert status == 200 and tx["ok"]
    return parent["id"], child["id"], today.month, today.year


def budget(base: str, month: int, year: int):
    status, data = api(base, f"/api/budget?month={month}&year={year}")
    assert status == 200
    return data


def basic_amount(base: str, category_id: int, month: int, year: int) -> float:
    status, groups = api(base, f"/api/categories?month={month}&year={year}")
    assert status == 200
    for group in groups:
        for category in ([group] if not group["children"] else group["children"]):
            if category["id"] == category_id:
                return float(category["basic"])
    raise AssertionError(f"category {category_id} not returned")


def additional_rows(base: str, category_id: int, month: int, year: int):
    rows = budget(base, month, year)["additionals"]
    return rows.get(str(category_id), rows.get(category_id, []))


def open_editor(page: Page, base: str, category_id: int) -> None:
    page.goto(base + "/m")
    page.wait_for_load_state("networkidle")
    page.locator('button[data-panel="panel-budget"]').click()
    group = page.locator("#m-cat-list .m-cat", has_text=LONG_PARENT)
    group.wait_for()
    group.click()
    page.locator(f'.m-cat-child[data-catid="{category_id}"]').click()
    page.locator("#m-budget-editor-title").wait_for()
    assert page.locator("#m-budget-editor-title").inner_text() == LONG_CATEGORY


def assert_tap_targets(page: Page) -> None:
    sizes = page.locator(
        "#m-budget-editor button:visible, #m-budget-editor input:visible"
    ).evaluate_all(
        "els => els.map(el => ({id: el.id || el.className, "
        "width: el.getBoundingClientRect().width, height: el.getBoundingClientRect().height}))"
    )
    too_small = [item for item in sizes if item["width"] < 43.5 or item["height"] < 43.5]
    assert not too_small, f"tap targets below 44x44: {too_small}"


def queue(page: Page):
    return page.evaluate("JSON.parse(localStorage.getItem('mb-offline-queue') || '[]')")


def screenshot(page: Page, shots: Path | None, name: str) -> None:
    if shots:
        page.screenshot(path=str(shots / name), full_page=True)


def run_browser(base: str, category_id: int, month: int, year: int, shots: Path | None) -> None:
    assert Path(CHROMIUM).is_file(), f"Chromium executable not found: {CHROMIUM}"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM, headless=True)
        context = browser.new_context(
            viewport=VIEWPORT,
            user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)",
            service_workers="block",
        )
        page = context.new_page()

        open_editor(page, base, category_id)
        assert page.viewport_size == VIEWPORT
        assert page.locator("#m-budget-basic-amount").input_value() == "123456.78"
        assert "654" in page.locator(".m-budget-transaction-amount").inner_text()
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        assert_tap_targets(page)
        screenshot(page, shots, "01-long-name-six-digit.png")

        # BASIC online save, followed by a fresh server read in the editor.
        page.locator("#m-budget-basic-amount").fill("123457.89")
        page.locator("#m-budget-basic-save").click()
        page.locator("#m-budget-basic-message").filter(has_text="BASIC saved").wait_for()
        assert basic_amount(base, category_id, month, year) == 123457.89
        assert page.locator("#m-budget-basic-amount").input_value() == "123457.89"

        # ADDITIONAL online add/delete including its note.
        page.locator("#m-budget-additional-amount").fill("275.40")
        page.locator("#m-budget-additional-note").fill("Winter shoes")
        page.locator("#m-budget-additional-add").click()
        row = page.locator(".m-budget-additional-row", has_text="Winter shoes")
        row.wait_for()
        rows = additional_rows(base, category_id, month, year)
        assert len(rows) == 1 and rows[0]["description"] == "Winter shoes"
        page.once("dialog", lambda dialog: dialog.accept())
        row.locator(".m-budget-additional-delete").click()
        page.locator("#m-budget-additional-message").filter(has_text="deleted").wait_for()
        assert additional_rows(base, category_id, month, year) == []

        # ADDITIONAL queues offline and replays after reconnect.
        context.set_offline(True)
        page.locator("#m-budget-additional-amount").fill("88.70")
        page.locator("#m-budget-additional-note").fill("Offline school supplies")
        page.locator("#m-budget-additional-add").click()
        page.locator("#m-budget-additional-message").filter(has_text="Queued").wait_for()
        queued = queue(page)
        assert len(queued) == 1
        assert queued[0]["method"] == "POST" and queued[0]["url"] == "/api/additional"
        context.set_offline(False)
        page.evaluate("flushQueue()")
        wait_until("offline ADDITIONAL replay", lambda: not queue(page))
        wait_until(
            "replayed ADDITIONAL in database",
            lambda: any(
                row["description"] == "Offline school supplies"
                for row in additional_rows(base, category_id, month, year)
            ),
        )
        page.locator(
            ".m-budget-additional-row", has_text="Offline school supplies"
        ).wait_for()

        # BASIC fails fast offline and leaves both the database and queue untouched.
        before_offline_basic = basic_amount(base, category_id, month, year)
        context.set_offline(True)
        page.locator("#m-budget-basic-amount").fill("222222.22")
        page.locator("#m-budget-basic-save").click()
        page.locator("#m-budget-basic-message").filter(has_text="not saved or queued").wait_for()
        assert queue(page) == []
        context.set_offline(False)
        assert basic_amount(base, category_id, month, year) == before_offline_basic
        page.wait_for_function(
            "expected => document.querySelector('#m-budget-basic-amount').value === expected",
            arg=f"{before_offline_basic:.2f}",
        )

        # Two independent phones load the same BASIC. The stale second write gets
        # 409, shows the current server value, and cannot overwrite the first.
        second_context = browser.new_context(
            viewport=VIEWPORT,
            user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)",
            service_workers="block",
        )
        second_page = second_context.new_page()
        open_editor(second_page, base, category_id)
        stale_expected = float(second_page.locator("#m-budget-basic-amount").input_value())
        assert stale_expected == before_offline_basic

        page.locator("#m-budget-basic-amount").fill("123500.01")
        page.locator("#m-budget-basic-save").click()
        page.locator("#m-budget-basic-message").filter(has_text="BASIC saved").wait_for()
        assert basic_amount(base, category_id, month, year) == 123500.01

        second_page.locator("#m-budget-basic-amount").fill("999999.99")
        second_page.locator("#m-budget-basic-save").click()
        conflict = second_page.locator("#m-budget-basic-message")
        conflict.filter(has_text="changed on another phone").wait_for()
        assert "123" in conflict.inner_text()
        assert second_page.locator("#m-budget-basic-amount").input_value() == "999999.99"
        assert basic_amount(base, category_id, month, year) == 123500.01
        assert queue(second_page) == []
        screenshot(second_page, shots, "02-basic-conflict.png")

        assert_tap_targets(page)
        second_context.close()
        context.close()
        browser.close()


def main() -> None:
    supplied_db = os.environ.get("BUDGET_DB")
    if supplied_db:
        assert_safe_database(resolved_db_path(supplied_db))

    shots = Path(os.environ["SHOTS"]).expanduser().resolve() if os.environ.get("SHOTS") else None
    if shots:
        shots.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="money-badger-mobile-budget-") as temp_dir:
        temp_path = Path(temp_dir).resolve()
        db_path = temp_path / "mobile-budget.db"
        assert_safe_database(db_path)
        port = free_port()
        base = f"http://127.0.0.1:{port}"
        env = os.environ.copy()
        env.update(
            {
                "BUDGET_DB": str(db_path),
                "MB_PASSWORD_HASH": "",
                "MB_API_TOKEN": "",
                "SECRET_KEY": "isolated-mobile-budget-ui-test",
            }
        )
        gunicorn = REPO / "venv" / "bin" / "gunicorn"
        assert gunicorn.is_file(), f"gunicorn not found: {gunicorn}"
        log_path = temp_path / "gunicorn.log"
        with log_path.open("w", encoding="utf-8") as server_log:
            server = subprocess.Popen(
                [str(gunicorn), "--workers", "1", "--bind", f"127.0.0.1:{port}", "app:app"],
                cwd=REPO,
                env=env,
                stdout=server_log,
                stderr=subprocess.STDOUT,
                text=True,
            )
            try:
                wait_until(
                    "isolated gunicorn",
                    lambda: api(base, "/api/categories")[0] == 200,
                    timeout=15,
                )
                _, category_id, month, year = seed(base)
                run_browser(base, category_id, month, year, shots)
            finally:
                server.terminate()
                try:
                    server.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=5)

    print("OK - isolated mobile BUDGET UI scenarios passed")


if __name__ == "__main__":
    main()
