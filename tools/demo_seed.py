#!/usr/bin/env python3
"""Fill a database with plausible, entirely invented data.

For screenshots and for trying the app out before committing your own numbers.
Refuses to touch a database that already has transactions.

    BUDGET_DB=/tmp/demo.db python3 tools/demo_seed.py
"""
import os
import random
import sqlite3
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import migrations  # noqa: E402
import presets  # noqa: E402

DB = os.environ.get('BUDGET_DB', '/tmp/money-badger-demo.db')

ACCOUNTS = [('Main account', 'bank'), ('Partner account', 'bank'),
            ('Cash', 'cash'), ('Savings', 'asset')]

# (category path, merchant, typical amount, times per month)
SPEND = [
    ('Groceries', 'Supermarket', 62, 9),
    ('Groceries', 'Corner shop', 18, 4),
    ('Dining', 'Cafe', 14, 5),
    ('Dining', 'Pizza place', 38, 2),
    ('Transport: Fuel', 'Petrol station', 78, 3),
    ('Transport: Public transport', 'City transit', 26, 2),
    ('Utilities: Electricity', 'Power company', 96, 1),
    ('Utilities: Internet', 'ISP', 42, 1),
    ('Housing: Rent / Mortgage', 'Mortgage', 940, 1),
    ('Health: Pharmacy', 'Pharmacy', 24, 2),
    ('Children: Childcare', 'Nursery', 310, 1),
    ('Personal: Clothing', 'Clothing store', 68, 1),
    ('Subscriptions: Streaming', 'Streaming service', 13, 1),
    ('Fun: Books', 'Bookshop', 22, 1),
    ('Pets: Food', 'Pet shop', 34, 1),
]

# Budgets sit on whatever level the spending does — a plan against a parent
# category whose children hold the transactions shows up as an empty column.
BUDGETS = {
    'Groceries': 700, 'Dining': 200,
    'Transport: Fuel': 260, 'Transport: Public transport': 60,
    'Utilities: Electricity': 110, 'Utilities: Internet': 45,
    'Housing: Rent / Mortgage': 950, 'Health: Pharmacy': 60,
    'Children: Childcare': 320, 'Personal: Clothing': 90,
    'Subscriptions: Streaming': 15, 'Fun: Books': 30, 'Pets: Food': 40,
}

MONTHS_BACK = 11


def main():
    fresh = not os.path.exists(DB)
    migrations.migrate(DB)
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row

    if db.execute('SELECT 1 FROM transactions LIMIT 1').fetchone():
        sys.exit(f'{DB} already has transactions — refusing to add demo data on top.')

    presets.apply_preset(db, presets.load_preset('household'), accounts=ACCOUNTS)
    for key, value in (('currency', '€'), ('locale', 'en-IE')):
        db.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)', (key, value))

    cat_id = {}
    for row in db.execute('''SELECT c.id, c.name AS name, p.name AS parent
                               FROM categories c LEFT JOIN categories p ON c.parent_id = p.id'''):
        cat_id[f"{row['parent']}: {row['name']}" if row['parent'] else row['name']] = row['id']

    income_id = {r['name']: r['id'] for r in db.execute('SELECT id, name FROM income_categories')}

    rnd = random.Random(20260813)  # fixed, so screenshots are reproducible
    today = date.today()
    n = 0

    for back in range(MONTHS_BACK, -1, -1):
        m = today.month - back
        y = today.year
        while m < 1:
            m += 12
            y -= 1
        last_day = 28 if m == 2 else (30 if m in (4, 6, 9, 11) else 31)
        cap = last_day if (y, m) != (today.year, today.month) else today.day

        for path, merchant, typical, per_month in SPEND:
            if path not in cat_id:
                continue
            for _ in range(per_month):
                day = rnd.randint(1, cap)
                amount = round(typical * rnd.uniform(0.6, 1.5), 2)
                account = rnd.choice(['Main account', 'Partner account', 'Cash'])
                db.execute(
                    '''INSERT INTO transactions (date, amount, account, description,
                                                 category_id, month, year, source_hash, tx_type)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'Expense')''',
                    (f'{y}-{m:02d}-{day:02d}', amount, account, merchant,
                     cat_id[path], m, y, f'demo-{n}'))
                n += 1

        for name, amount in (('Salary', 3200), ('Partner salary', 2750)):
            if name not in income_id:
                continue
            got = round(amount * rnd.uniform(0.97, 1.06), 2)
            db.execute(
                '''INSERT INTO transactions (date, amount, account, description, income_category_id,
                                             month, year, source_hash, tx_type)
                   VALUES (?, ?, 'Main account', ?, ?, ?, ?, ?, 'Income')''',
                (f'{y}-{m:02d}-26', got, name, income_id[name], m, y, f'demo-{n}'))
            n += 1
            db.execute(
                '''INSERT INTO income_entries (category_id, month, year, planned, received)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(category_id, month, year) DO UPDATE SET
                     planned=excluded.planned, received=excluded.received''',
                (income_id[name], m, y, amount, got))

    for name, amount in BUDGETS.items():
        if name in cat_id:
            db.execute('INSERT OR REPLACE INTO basic_budgets (category_id, amount) VALUES (?, ?)',
                       (cat_id[name], amount))

    for i, (desc, amount, done) in enumerate([
            ('New boiler', 3200, 1), ('Kitchen worktop', 2400, 1), ('Bathroom retile', 4100, 0),
            ('Garden fence', 1500, 0), ('Car service', 900, 0)]):
        db.execute('''INSERT INTO big_expenses (description, amount, done, year, sort_order)
                      VALUES (?, ?, ?, ?, ?)''', (desc, amount, done, today.year, i))

    # A couple of unreviewed rows so the CHECK ME tab isn't empty in screenshots.
    for i, (desc, amount) in enumerate([('CARD PAYMENT 4829', 47.30), ('TRANSFER REF 90210', 120.00)]):
        d = today - timedelta(days=i + 1)
        db.execute('''INSERT INTO transactions (date, amount, account, description, month, year,
                                                source_hash, tx_type)
                      VALUES (?, ?, 'Main account', ?, ?, ?, ?, 'Expense')''',
                   (d.isoformat(), amount, desc, d.month, d.year, f'demo-check-{i}'))

    for name, balance in (('Main account', 2840.55), ('Partner account', 1190.20),
                          ('Cash', 85.00), ('Savings', 12500.00)):
        db.execute('UPDATE accounts SET balance=?, balance_anchor_date=? WHERE name=?',
                   (balance, today.isoformat(), name))

    db.commit()
    total = db.execute('SELECT count(*) FROM transactions').fetchone()[0]
    db.close()
    print(f"{'created' if fresh else 'filled'} {DB}: {total} transactions across {MONTHS_BACK + 1} months")


if __name__ == '__main__':
    main()
