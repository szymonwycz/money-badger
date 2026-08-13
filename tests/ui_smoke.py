"""UI smoke test for Money Badger — desktop pages, mobile PWA flow, overview chart.

Run against a live instance:
    BASE=http://127.0.0.1:5055 SHOTS=shots python tests/ui_smoke.py

Doubles as the screenshot generator for the README (SHOTS=docs/img).
"""
import os
import sys
from datetime import date
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = os.environ.get('BASE', 'http://127.0.0.1:5055')
SHOTS = os.environ.get('SHOTS', 'shots')
Path(SHOTS).mkdir(parents=True, exist_ok=True)

errors = []

def check(name, cond, detail=''):
    status = 'OK ' if cond else 'FAIL'
    print(f'[{status}] {name} {detail}')
    if not cond:
        errors.append(name)

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)

    # ── desktop pages ──
    page = browser.new_page(viewport={'width': 1280, 'height': 900})
    console_errors = []
    page.on('console', lambda m: console_errors.append(f'{m.type}: {m.text}') if m.type == 'error' else None)
    page.on('pageerror', lambda e: console_errors.append(f'pageerror: {e}'))

    for path, name in [('/', 'expenses'), ('/income', 'income'), ('/check-me', 'check_me'),
                       ('/transactions', 'transactions'), ('/large-expenses', 'large_expenses'),
                       ('/overview', 'overview'), ('/calculator', 'calculator')]:
        page.goto(BASE + path)
        page.wait_for_load_state('networkidle')
        page.screenshot(path=f'{SHOTS}/desktop_{name}.png', full_page=True)
        check(f'page {path}', True)

    # overview chart rendered?
    page.goto(BASE + '/overview')
    page.wait_for_load_state('networkidle')
    bars = page.locator('#ov-chart rect[fill="var(--yellow)"]').count()
    check('overview: spent bars rendered', bars > 0, f'({bars} bars)')
    rows = page.locator('#ov-table tr').count()
    check('overview: category table rows', rows > 2, f'({rows} rows)')

    # transactions: add + delete round-trip
    page.goto(BASE + '/transactions')
    page.wait_for_load_state('networkidle')
    before = page.locator('#tx-count').inner_text()
    # Must land in the month the page is showing, which is the current one — a fixed
    # date silently stopped being visible the moment that month rolled over.
    page.fill('#new-date', date.today().isoformat())
    page.fill('#new-amount', '12.34')
    page.fill('#new-desc', 'UI-TEST-DELETE-ME')
    page.click('#btn-add-tx')
    page.wait_for_timeout(800)
    row_sel = 'tr:has(input[value="UI-TEST-DELETE-ME"])'
    added = page.locator(row_sel).count()
    check('transactions: add works', added >= 1)
    # delete every UI-test row (including leftovers from previous runs)
    while page.locator(row_sel).count():
        page.once('dialog', lambda d: d.accept())
        page.locator(row_sel).first.locator('.tx-del').click()
        page.wait_for_timeout(600)
    check('transactions: delete works', page.locator(row_sel).count() == 0)

    check('desktop: no console errors', not console_errors, str(console_errors[:3]))

    # ── mobile view ──
    m = browser.new_page(viewport={'width': 390, 'height': 844},
                         user_agent='Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)')
    m_console = []
    m.on('console', lambda msg: m_console.append(f'{msg.type}: {msg.text}') if msg.type == 'error' else None)
    m.on('pageerror', lambda e: m_console.append(f'pageerror: {e}'))

    m.goto(BASE + '/m')
    m.wait_for_load_state('networkidle')
    m.screenshot(path=f'{SHOTS}/mobile_add.png')
    check('mobile: category select populated', m.locator('#m-category option').count() > 5)
    check('mobile: account select populated', m.locator('#m-account option').count() >= 3)

    # add expense on mobile
    m.fill('#m-amount', '9.99')
    m.select_option('#m-category', label='Groceries')
    m.fill('#m-desc', 'MOBILE-UI-TEST')
    m.click('#m-add-btn')
    m.wait_for_timeout(800)
    status = m.locator('#m-add-status').inner_text()
    check('mobile: expense added', 'Added' in status, status)

    # balances tab
    m.click('button[data-panel="panel-balances"]')
    m.wait_for_timeout(600)
    m.screenshot(path=f'{SHOTS}/mobile_balances.png')
    check('mobile: balances rows', m.locator('#m-bal-accounts .m-bal-row').count() >= 3)

    # budget tab
    m.click('button[data-panel="panel-budget"]')
    m.wait_for_timeout(800)
    m.screenshot(path=f'{SHOTS}/mobile_budget.png')
    check('mobile: budget categories', m.locator('#m-cat-list .m-cat').count() > 3)

    check('mobile: no console errors', not m_console, str(m_console[:3]))

    # clean up the mobile test transaction via API
    import urllib.request, json
    txs = json.load(urllib.request.urlopen(f'{BASE}/api/transactions?month=7&year=2026'))
    for t in txs:
        if t['description'] == 'MOBILE-UI-TEST':
            req = urllib.request.Request(f'{BASE}/api/transactions/{t["id"]}', method='DELETE')
            urllib.request.urlopen(req)
            print(f'cleaned up mobile test tx id={t["id"]}')

    browser.close()

print()
print('ERRORS:', errors if errors else 'none')
sys.exit(1 if errors else 0)
