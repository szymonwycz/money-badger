"""Isolated Playwright coverage for goal mapping on desktop and mobile.

Run manually (never against the production process/database):
    venv/bin/python tests/ui_goal_modal.py

The script starts its own gunicorn on a free localhost port, seeds only through
the public API, and creates BUDGET_DB inside a temporary directory. CHROMIUM may
override /usr/bin/chromium.
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
MAPPED_DESKTOP = "Mapped goal desktop sentinel"
MAPPED_MOBILE = "Mapped goal mobile sentinel"
UNMAPPED = "Unmapped goal sentinel"
GOAL_NAME = "UI Savings Goal"
MOBILE_VIEWPORT = {"width": 390, "height": 844}


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
    request = urllib.request.Request(
        base + path,
        data=body,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
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


def test_iban(serial: int) -> str:
    bban = f"{serial:024d}"
    check = 98 - (int(f"{bban}252100") % 97)
    return f"PL{check:02d}{bban}"


def seed(base: str) -> int:
    status, category = api(base, "/api/categories", "POST", {"name": "UI Test"})
    assert status == 200 and category["ok"]
    for name, account_type in (("UI Current", "bank"), (GOAL_NAME, "asset")):
        status, result = api(
            base, "/api/accounts", "POST", {"name": name, "type": account_type}
        )
        assert status == 200 and result["ok"]

    status, accounts = api(base, "/api/accounts")
    assert status == 200
    goal_id = next(account["id"] for account in accounts if account["name"] == GOAL_NAME)
    mapped_iban = test_iban(9001)
    today = date.today().strftime("%d/%m/%Y")

    # Public Money Badger creates an IBAN mapping only while resolving a CHECK ME
    # transaction. Seed through that public workflow instead of writing the DB or
    # relying on a private-only account endpoint.
    bootstrap_description = "Goal mapping bootstrap sentinel"
    status, result = api(base, "/api/transactions/bulk", "POST", [{
        "date": today,
        "amount": 1,
        "account": "UI Current",
        "category": "CHECK ME",
        "description": bootstrap_description,
        "counterpart_iban": mapped_iban,
        "hash": "ui-goal-map-bootstrap",
    }])
    assert status == 200 and result["inserted"] == 1
    status, unreviewed = api(base, "/api/transactions/unreviewed")
    assert status == 200
    bootstrap_id = next(
        row["id"] for row in unreviewed["expenses"]
        if row["description"] == bootstrap_description
    )
    status, result = api(
        base,
        f"/api/transactions/{bootstrap_id}/goal-transfer",
        "POST",
        {"account_id": goal_id},
    )
    assert status == 200 and result["ok"]

    rows = [
        (MAPPED_DESKTOP, mapped_iban, "ui-goal-desktop"),
        (MAPPED_MOBILE, mapped_iban, "ui-goal-mobile"),
        (UNMAPPED, test_iban(9002), "ui-goal-unmapped"),
    ]
    payload = [
        {
            "date": today,
            "amount": 10 + index,
            "account": "UI Current",
            "category": "CHECK ME",
            "description": description,
            "counterpart_iban": iban,
            "hash": source_hash,
        }
        for index, (description, iban, source_hash) in enumerate(rows)
    ]
    status, result = api(base, "/api/transactions/bulk", "POST", payload)
    assert status == 200 and result["inserted"] == len(payload)
    return goal_id


def assert_transfer(base: str, description: str) -> None:
    today = date.today()
    status, rows = api(base, f"/api/transactions?month={today.month}&year={today.year}")
    assert status == 200
    row = next(item for item in rows if description in item["description"])
    assert row["tx_type"] == "Money Transfer"
    assert row["account"] == "UI Current"
    assert row["account_to"] == GOAL_NAME


def assert_mapped_select(select, goal_id: int) -> None:
    assert select.input_value() == str(goal_id)
    assert "IBAN match" in select.locator("option:checked").inner_text()
    assert select.locator('option[value="__new__"]').count() == 0
    assert "Create a new goal account" not in select.inner_text()


def assert_unmapped_select(select) -> None:
    assert select.input_value() == ""
    assert select.locator('option[value="__new__"]').count() == 1
    assert "Create a new goal account" in select.inner_text()


def run_desktop(page: Page, base: str, goal_id: int, shots: Path | None) -> None:
    page.goto(base + "/check-me")
    page.wait_for_load_state("networkidle")

    mapped = page.locator("#check-me-expenses-body tr", has_text=MAPPED_DESKTOP)
    mapped.locator(".btn-goal").click()
    select = page.locator("#goal-account-select")
    assert_mapped_select(select, goal_id)
    if shots:
        page.locator("#goal-modal").screenshot(path=str(shots / "goal_modal_desktop.png"))
    page.locator("#goal-map-submit").click()
    mapped.wait_for(state="detached")
    assert_transfer(base, MAPPED_DESKTOP)

    unmapped = page.locator("#check-me-expenses-body tr", has_text=UNMAPPED)
    unmapped.locator(".btn-goal").click()
    assert_unmapped_select(select)
    page.locator("#goal-modal-close").click()


def run_mobile(page: Page, base: str, goal_id: int, shots: Path | None) -> None:
    page.goto(base + "/m")
    page.wait_for_load_state("networkidle")
    page.locator('button[data-panel="panel-check"]').click()

    mapped = page.locator("#m-check-list .m-check-item", has_text=MAPPED_MOBILE)
    mapped.wait_for()
    mapped.locator(".m-check-goal-toggle").click()
    select = mapped.locator(".m-check-goal-account")
    assert_mapped_select(select, goal_id)
    if shots:
        mapped.screenshot(path=str(shots / "goal_modal_mobile.png"))
    mapped.locator(".m-check-goal-submit").click()
    mapped.wait_for(state="detached")
    assert_transfer(base, MAPPED_MOBILE)

    unmapped = page.locator("#m-check-list .m-check-item", has_text=UNMAPPED)
    unmapped.locator(".m-check-goal-toggle").click()
    assert_unmapped_select(unmapped.locator(".m-check-goal-account"))


def run_browser(base: str, goal_id: int) -> None:
    assert Path(CHROMIUM).is_file(), f"Chromium executable not found: {CHROMIUM}"
    shots = None
    if os.environ.get("SHOTS"):
        shots = Path(os.environ["SHOTS"]).expanduser().resolve()
        shots.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM, headless=True)
        desktop = browser.new_page()
        run_desktop(desktop, base, goal_id, shots)
        desktop.close()

        mobile_context = browser.new_context(
            viewport=MOBILE_VIEWPORT,
            user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)",
            service_workers="block",
        )
        mobile = mobile_context.new_page()
        run_mobile(mobile, base, goal_id, shots)
        mobile_context.close()
        browser.close()


def main() -> None:
    supplied_db = os.environ.get("BUDGET_DB")
    if supplied_db:
        assert_safe_database(resolved_db_path(supplied_db))

    with tempfile.TemporaryDirectory(prefix="money-badger-goal-modal-") as temp_dir:
        temp_path = Path(temp_dir).resolve()
        db_path = temp_path / "goal-modal.db"
        assert_safe_database(db_path)
        port = free_port()
        base = f"http://127.0.0.1:{port}"
        env = os.environ.copy()
        env.update({
            "BUDGET_DB": str(db_path),
            "MB_PASSWORD_HASH": "",
            "MB_API_TOKEN": "",
            "SECRET_KEY": "isolated-goal-modal-ui-test",
        })
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
                goal_id = seed(base)
                run_browser(base, goal_id)
            finally:
                server.terminate()
                try:
                    server.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=5)

    print("OK - isolated desktop and mobile goal modal scenarios passed")


if __name__ == "__main__":
    main()
