import os, sys, json, urllib.request
from pathlib import Path
from playwright.sync_api import sync_playwright
BASE = os.environ.get('BASE', 'http://127.0.0.1:5055')
SHOTS = os.environ.get('SHOTS', 'shots')
# Runs against a live instance, so it needs two real account names from that
# install: ACCT=<from> ACCT_TO=<to> python tests/ui_transfer.py
ACCT    = os.environ.get('ACCT', 'Main')
ACCT_TO = os.environ.get('ACCT_TO', 'Cash')
Path(SHOTS).mkdir(parents=True, exist_ok=True)
errors = []
def check(n, c, d=''):
    print(f'[{"OK " if c else "FAIL"}] {n} {d}')
    if not c: errors.append(n)

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    m = b.new_page(viewport={'width': 390, 'height': 844})
    con = []
    m.on('pageerror', lambda e: con.append(str(e)))
    m.goto(BASE + '/m'); m.wait_for_load_state('networkidle')

    m.click('#m-type-transfer'); m.wait_for_timeout(300)
    m.screenshot(path=f'{SHOTS}/mobile_transfer.png')
    check('toggle: category hidden', not m.locator('#m-category-field').is_visible())
    check('toggle: to-account shown', m.locator('#m-account-to-field').is_visible())
    check('toggle: button label', m.locator('#m-add-btn').inner_text() == 'ADD TRANSFER')

    m.fill('#m-amount', '123')
    m.select_option('#m-account', ACCT)
    m.select_option('#m-account-to', ACCT_TO)
    m.fill('#m-desc', 'TRANSFER-UI-TEST')
    m.click('#m-add-btn'); m.wait_for_timeout(700)
    check('transfer: added', 'Added' in m.locator('#m-add-status').inner_text())

    # same-account guard
    m.fill('#m-amount', '5')
    m.select_option('#m-account', ACCT)
    m.select_option('#m-account-to', ACCT)
    m.click('#m-add-btn'); m.wait_for_timeout(400)
    check('transfer: same-account blocked', 'different' in m.locator('#m-add-status').inner_text())

    # verify in API + detail view is transfer-safe (readonly desc)
    txs = json.load(urllib.request.urlopen(f'{BASE}/api/transactions?month=7&year=2026'))
    t = next((x for x in txs if 'TRANSFER-UI-TEST' in (x['description'] or '')), None)
    check('api: transfer persisted', t is not None and t['tx_type'] == 'Money Transfer' and t['account_to'] == ACCT_TO)

    m.click('button[data-panel="panel-txns"]'); m.wait_for_timeout(700)
    m.locator('.m-tx-row', has_text='123').first.click(); m.wait_for_timeout(400)
    check('detail: in-flow panel open', m.locator('#m-tx-detail.open').is_visible())
    check('detail: txns list hidden', not m.locator('#panel-txns').is_visible())
    check('detail: transfer desc readonly', m.locator('#m-tx-desc').get_attribute('readonly') is not None)
    check('detail: no category fields', not m.locator('#m-tx-cat-field').is_visible())
    m.screenshot(path=f'{SHOTS}/mobile_transfer_detail.png')

    # cleanup
    m.once('dialog', lambda d: d.accept())
    m.click('#m-tx-delete'); m.wait_for_timeout(600)
    txs = json.load(urllib.request.urlopen(f'{BASE}/api/transactions?month=7&year=2026'))
    check('cleanup: deleted', not any('TRANSFER-UI-TEST' in (x['description'] or '') for x in txs))
    check('no js errors', not con, str(con[:2]))
    b.close()
print('\nERRORS:', errors if errors else 'none')
sys.exit(1 if errors else 0)
