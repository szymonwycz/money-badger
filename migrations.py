"""Versioned schema migrations for Money Badger.

Each migration runs once, in order, and is recorded in `schema_migrations`.
To change the schema: append a new (name, function) pair to MIGRATIONS —
never edit an already-shipped migration.

The baseline (0001) is idempotent on purpose: existing production databases
predate this system, so the first run against them must be a safe no-op.

Migrations build the schema and nothing else. Starter categories, income
categories and accounts live in presets/ and are applied by the setup screen —
see presets.py. A fresh database therefore comes up empty, which is what sends
a first-time user to /setup.
"""
import sqlite3
from datetime import datetime


def _0001_baseline(db):
    """Full schema + seeds as of 2026-07 (pre-migration-system state). Idempotent."""
    db.executescript('''
        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY,
            parent_id INTEGER REFERENCES categories(id),
            name TEXT NOT NULL,
            sort_order INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS basic_budgets (
            category_id INTEGER PRIMARY KEY REFERENCES categories(id),
            amount REAL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS basic_budgets_monthly (
            category_id INTEGER NOT NULL REFERENCES categories(id),
            month INTEGER NOT NULL,
            year INTEGER NOT NULL,
            amount REAL DEFAULT 0,
            PRIMARY KEY (category_id, month, year)
        );
        CREATE TABLE IF NOT EXISTS additional_budgets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category_id INTEGER NOT NULL REFERENCES categories(id),
            amount REAL NOT NULL,
            description TEXT DEFAULT '',
            month INTEGER NOT NULL,
            year INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS big_expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            description TEXT NOT NULL,
            amount REAL NOT NULL,
            done INTEGER DEFAULT 0,
            year INTEGER NOT NULL,
            sort_order INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            amount REAL NOT NULL,
            account TEXT DEFAULT '',
            description TEXT DEFAULT '',
            category_id INTEGER REFERENCES categories(id),
            month INTEGER NOT NULL,
            year INTEGER NOT NULL,
            source_hash TEXT UNIQUE
        );
        CREATE TABLE IF NOT EXISTS corrections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tx_id INTEGER NOT NULL REFERENCES transactions(id),
            old_category_id INTEGER REFERENCES categories(id),
            new_category_id INTEGER REFERENCES categories(id),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            synced INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            balance REAL DEFAULT 0,
            type TEXT DEFAULT 'bank',
            iban TEXT,
            updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS monthly_income (
            month INTEGER NOT NULL,
            year INTEGER NOT NULL,
            amount REAL DEFAULT 0,
            note TEXT DEFAULT '',
            PRIMARY KEY (month, year)
        );
        CREATE TABLE IF NOT EXISTS lot_plans (
            month INTEGER NOT NULL,
            year INTEGER NOT NULL,
            nalot_minutes INTEGER DEFAULT 0,
            nalot_rate REAL DEFAULT 0,
            silnik_count INTEGER DEFAULT 0,
            silnik_rate REAL DEFAULT 0,
            stby_count INTEGER DEFAULT 0,
            stby_rate REAL DEFAULT 0,
            PRIMARY KEY (month, year)
        );
        CREATE TABLE IF NOT EXISTS lot_adjustments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            month INTEGER NOT NULL,
            year INTEGER NOT NULL,
            sign INTEGER NOT NULL,
            minutes INTEGER NOT NULL,
            description TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS income_categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            sort_order INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS income_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category_id INTEGER NOT NULL REFERENCES income_categories(id),
            month INTEGER NOT NULL,
            year INTEGER NOT NULL,
            planned REAL DEFAULT 0,
            received REAL DEFAULT 0,
            note TEXT DEFAULT '',
            UNIQUE(category_id, month, year)
        );
    ''')

    # column additions that predate this migration system (no-op if present)
    for alter in [
        'ALTER TABLE big_expenses ADD COLUMN planned_month INTEGER',
        'ALTER TABLE transactions ADD COLUMN tx_type TEXT DEFAULT "Expense"',
        'ALTER TABLE transactions ADD COLUMN account_to TEXT DEFAULT ""',
        'ALTER TABLE transactions ADD COLUMN income_category_id INTEGER REFERENCES income_categories(id)',
    ]:
        try:
            db.execute(alter)
        except sqlite3.OperationalError:
            pass

    acct_cols = [r['name'] for r in db.execute("PRAGMA table_info(accounts)").fetchall()]
    if 'balance_anchor_tx_id' not in acct_cols:
        db.execute('ALTER TABLE accounts ADD COLUMN balance_anchor_tx_id INTEGER DEFAULT 0')
        max_id = db.execute('SELECT COALESCE(MAX(id), 0) FROM transactions').fetchone()[0]
        db.execute('UPDATE accounts SET balance_anchor_tx_id=?', (max_id,))

    # 'savings' was renamed to 'asset' before this migration system existed
    db.execute("UPDATE accounts SET type='asset' WHERE type='savings'")


def _0002_indexes(db):
    """Query-path indexes: every list endpoint filters by month/year or account."""
    db.executescript('''
        CREATE INDEX IF NOT EXISTS idx_tx_month_year   ON transactions(year, month);
        CREATE INDEX IF NOT EXISTS idx_tx_category     ON transactions(category_id);
        CREATE INDEX IF NOT EXISTS idx_tx_account      ON transactions(account);
        CREATE INDEX IF NOT EXISTS idx_addl_month_year ON additional_budgets(year, month);
        CREATE INDEX IF NOT EXISTS idx_inc_month_year  ON income_entries(year, month);
        CREATE INDEX IF NOT EXISTS idx_corr_synced     ON corrections(synced);
    ''')


def _0003_remove_resurrected_accounts(db):
    """Accounts deleted by the user on 2026-07-01 kept coming back: the old
    init_db re-INSERTed them on every service restart. Remove them again —
    but only while they're empty, in case real money shows up there first."""
    for name in ('Euro RTV AGD', 'Ogród', 'Ściany na piętrze'):
        acct = db.execute('SELECT id, balance FROM accounts WHERE name=?', (name,)).fetchone()
        if not acct or (acct['balance'] or 0) != 0:
            continue
        real_tx = db.execute(
            '''SELECT 1 FROM transactions
               WHERE (account=? OR account_to=?) AND tx_type != 'Balance Adjust' LIMIT 1''',
            (name, name)).fetchone()
        if real_tx:
            continue
        db.execute("DELETE FROM transactions WHERE account=? AND tx_type='Balance Adjust'", (name,))
        db.execute('DELETE FROM accounts WHERE id=?', (acct['id'],))


def _0004_ignored_flag(db):
    """CHECK ME 'ignore' button: dismiss a transaction from the review queue
    without forcing a category on it."""
    db.execute('ALTER TABLE transactions ADD COLUMN ignored INTEGER DEFAULT 0')


def _0005_balance_anchor_date(db):
    """The balance checkpoint anchored on transaction id alone, and id is insertion
    order, not transaction date. The bank posts some transactions days late, so a
    charge dated BEFORE a manual balance reading can be imported AFTER it, land with
    id > anchor, and be subtracted from a balance that already included it.

    Record the date the checkpoint was taken so that case can be excluded.
    Backfilled from the anchor row's own date (a manual correction writes a
    Balance Adjust row dated that day)."""
    db.execute('ALTER TABLE accounts ADD COLUMN balance_anchor_date TEXT')
    db.execute('''UPDATE accounts
                     SET balance_anchor_date = (
                         SELECT t.date FROM transactions t
                          WHERE t.id = accounts.balance_anchor_tx_id)
                   WHERE balance_anchor_tx_id IS NOT NULL
                     AND balance_anchor_tx_id > 0''')


def _0006_settings(db):
    """Instance-wide preferences that aren't per-month data: currency, locale, and
    (from 0007) how the calculator tab is wired up. Seeded with what the app did
    when these were hardcoded, so an upgrade changes nothing on screen."""
    db.execute('''CREATE TABLE IF NOT EXISTS settings (
                      key TEXT PRIMARY KEY,
                      value TEXT NOT NULL)''')
    for key, value in (('currency', 'zł'), ('locale', 'pl-PL')):
        db.execute('INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)', (key, value))


def _0007_calculator(db):
    """Replace the airline-pay calculator with a configurable one.

    The old tables named their columns after one pilot's contract — nalot_minutes,
    silnik_count, stby_count — and the tax rates and destination categories were
    literals in both the frontend and the backend. Here a user defines any number
    of line items (name, unit, rate) and the rates and target categories are
    settings, so the same tab serves hourly, daily or per-project work.

    Existing lot_plans rows carry over as three items with their monthly values,
    and the settings are seeded with the previous Polish literals so the numbers
    on screen don't move.
    """
    db.executescript('''
        CREATE TABLE IF NOT EXISTS calc_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            unit TEXT DEFAULT '',
            time_input INTEGER DEFAULT 0,
            rate REAL DEFAULT 0,
            sort_order INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS calc_entries (
            item_id INTEGER NOT NULL REFERENCES calc_items(id),
            month INTEGER NOT NULL,
            year INTEGER NOT NULL,
            qty REAL DEFAULT 0,
            rate REAL DEFAULT 0,
            PRIMARY KEY (item_id, month, year)
        );
        CREATE TABLE IF NOT EXISTS calc_adjustments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id INTEGER REFERENCES calc_items(id),
            month INTEGER NOT NULL,
            year INTEGER NOT NULL,
            sign INTEGER NOT NULL,
            qty REAL NOT NULL,
            description TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_calc_entries_month ON calc_entries(year, month);
    ''')

    has_lot = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='lot_plans'").fetchone()
    plans = db.execute('SELECT * FROM lot_plans ORDER BY year, month').fetchall() if has_lot else []
    if not plans:
        return  # fresh install: the setup screen defines the items

    # Rates default to the most recent month's, which is what a new month inherits.
    latest = plans[-1]
    specs = [
        ('Flight hours', 'h',   1, latest['nalot_rate'],  'nalot_minutes', 'nalot_rate'),
        ('Engine runs',  'ea',  0, latest['silnik_rate'], 'silnik_count',  'silnik_rate'),
        ('Standby',      'day', 0, latest['stby_rate'],   'stby_count',    'stby_rate'),
    ]
    item_ids = {}
    for order, (name, unit, time_input, rate, qty_col, rate_col) in enumerate(specs):
        cur = db.execute(
            'INSERT INTO calc_items (name, unit, time_input, rate, sort_order) VALUES (?, ?, ?, ?, ?)',
            (name, unit, time_input, rate or 0, order))
        item_ids[name] = cur.lastrowid
        for p in plans:
            # Flight hours were stored as minutes; every item now holds its own unit.
            qty = (p[qty_col] or 0) / 60 if qty_col == 'nalot_minutes' else (p[qty_col] or 0)
            db.execute(
                'INSERT INTO calc_entries (item_id, month, year, qty, rate) VALUES (?, ?, ?, ?, ?)',
                (item_ids[name], p['month'], p['year'], qty, p[rate_col] or 0))

    for a in db.execute('SELECT * FROM lot_adjustments').fetchall():
        db.execute(
            '''INSERT INTO calc_adjustments (item_id, month, year, sign, qty, description, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)''',
            (item_ids['Flight hours'], a['month'], a['year'], a['sign'],
             (a['minutes'] or 0) / 60, a['description'], a['created_at']))

    # Defaults for an install being upgraded from the pre-1.0 calculator, which
    # had these as literals in the code. A fresh database has no legacy plans and
    # returned above, so nothing here reaches a new user — and every one of these
    # is editable in the calculator settings afterwards.
    for key, value in (
        ('calc.enabled', '1'),
        ('calc.tab_label', 'CALCULATOR'),
        ('calc.vat_rate', '0.23'),
        ('calc.vat_label', 'VAT'),
        ('calc.tax_rate', '0.085'),
        ('calc.tax_label', 'PIT'),
        ('calc.income_category', 'Salary'),
        ('calc.vat_category', 'Taxes: VAT'),
        ('calc.tax_category', 'Taxes: Income tax'),
        ('calc.month_offset', '1'),
        ('calc.apply_note', 'calculator'),
    ):
        db.execute('INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)', (key, value))


def _0008_drop_lot_tables(db):
    """The calculator no longer reads them; 0007 copied everything across."""
    db.execute('DROP TABLE IF EXISTS lot_adjustments')
    db.execute('DROP TABLE IF EXISTS lot_plans')


MIGRATIONS = [
    ('0001_baseline', _0001_baseline),
    ('0002_indexes', _0002_indexes),
    ('0003_remove_resurrected_accounts', _0003_remove_resurrected_accounts),
    ('0004_ignored_flag', _0004_ignored_flag),
    ('0005_balance_anchor_date', _0005_balance_anchor_date),
    ('0006_settings', _0006_settings),
    ('0007_calculator', _0007_calculator),
    ('0008_drop_lot_tables', _0008_drop_lot_tables),
]


def migrate(db_path):
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys = ON')
    db.execute('PRAGMA journal_mode = WAL')  # readers (backups) don't block the app
    db.execute('''CREATE TABLE IF NOT EXISTS schema_migrations (
                      name TEXT PRIMARY KEY,
                      applied_at TEXT NOT NULL)''')
    applied = {r['name'] for r in db.execute('SELECT name FROM schema_migrations').fetchall()}
    for name, fn in MIGRATIONS:
        if name in applied:
            continue
        fn(db)
        db.execute('INSERT INTO schema_migrations (name, applied_at) VALUES (?, ?)',
                   (name, datetime.now().isoformat(timespec='seconds')))
        db.commit()
    db.close()


if __name__ == '__main__':
    import os
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else os.environ.get(
        'BUDGET_DB', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'budget.db'))
    migrate(path)
    print(f'migrations up to date: {path}')
