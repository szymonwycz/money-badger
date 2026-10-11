"""First-run setup flow, against an empty database."""
import os
import sqlite3
import sys
import tempfile

import pytest

TEST_DB = os.path.join(tempfile.mkdtemp(prefix='money-badger-setup-'), 'test.db')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import migrations  # noqa: E402
import presets  # noqa: E402

migrations.migrate(TEST_DB)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as badger  # noqa: E402


@pytest.fixture()
def client(monkeypatch):
    """Point the already-imported app at this module's empty database."""
    monkeypatch.setattr(badger, 'DB_PATH', TEST_DB)
    badger.app.config['TESTING'] = True
    with badger.app.test_client() as c:
        yield c


@pytest.fixture()
def db():
    conn = sqlite3.connect(TEST_DB)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


def test_migrations_leave_the_database_empty(db):
    """Nothing is seeded any more — that is what sends a new user to /setup."""
    assert db.execute('SELECT count(*) FROM categories').fetchone()[0] == 0
    assert db.execute('SELECT count(*) FROM accounts').fetchone()[0] == 0


def test_counterpart_iban_migration_upgrades_existing_database_idempotently(tmp_path):
    path = tmp_path / 'existing.db'
    conn = sqlite3.connect(path)
    conn.execute('''CREATE TABLE schema_migrations (
                        name TEXT PRIMARY KEY,
                        applied_at TEXT NOT NULL)''')
    conn.execute('CREATE TABLE transactions (id INTEGER PRIMARY KEY)')
    conn.execute('CREATE TABLE accounts (id INTEGER PRIMARY KEY, iban TEXT)')
    conn.executemany(
        'INSERT INTO schema_migrations (name, applied_at) VALUES (?, ?)',
        [(name, '2032-10-09') for name, _fn in migrations.MIGRATIONS
         if name < '0010_transaction_counterpart_iban'],
    )
    conn.commit()
    conn.close()

    migrations.migrate(path)
    migrations.migrate(path)

    conn = sqlite3.connect(path)
    columns = {row[1] for row in conn.execute('PRAGMA table_info(transactions)')}
    applied = conn.execute(
        "SELECT COUNT(*) FROM schema_migrations "
        "WHERE name='0010_transaction_counterpart_iban'"
    ).fetchone()[0]
    conn.close()
    assert 'counterpart_iban' in columns
    assert applied == 1


def test_account_iban_migration_creates_a_partial_unique_index(tmp_path):
    path = tmp_path / 'iban-index.db'
    migrations.migrate(path)
    conn = sqlite3.connect(path)
    indexes = {row[1] for row in conn.execute("PRAGMA index_list(accounts)")}
    assert 'idx_accounts_iban_unique' in indexes
    conn.execute("INSERT INTO accounts (name, iban) VALUES ('First', 'GB82WEST12345698765432')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO accounts (name, iban) VALUES ('Second', 'GB82WEST12345698765432')")
    conn.execute("INSERT INTO accounts (name, iban) VALUES ('No IBAN 1', NULL)")
    conn.execute("INSERT INTO accounts (name, iban) VALUES ('No IBAN 2', NULL)")
    conn.close()


def test_non_goal_iban_migration_creates_decision_table(tmp_path):
    path = tmp_path / 'non-goal.db'
    migrations.migrate(path)
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO non_goal_ibans (iban) VALUES ('GB29NWBK60161331926819')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO non_goal_ibans (iban) VALUES ('GB29NWBK60161331926819')")
    conn.close()


def test_goal_discovery_candidate_migration_defaults_existing_rows_false(tmp_path):
    path = tmp_path / 'goal-candidate.db'
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute('''CREATE TABLE schema_migrations (
                        name TEXT PRIMARY KEY,
                        applied_at TEXT NOT NULL)''')
    conn.execute('CREATE TABLE transactions (id INTEGER PRIMARY KEY)')
    conn.execute('INSERT INTO transactions DEFAULT VALUES')
    conn.executemany(
        'INSERT INTO schema_migrations (name, applied_at) VALUES (?, ?)',
        [(name, '2032-10-09') for name, _fn in migrations.MIGRATIONS
         if name < '0013_goal_discovery_candidate'],
    )
    conn.commit()
    conn.close()

    migrations.migrate(path)
    migrations.migrate(path)

    conn = sqlite3.connect(path)
    value = conn.execute(
        'SELECT goal_discovery_candidate FROM transactions WHERE id=1'
    ).fetchone()[0]
    applied = conn.execute(
        "SELECT COUNT(*) FROM schema_migrations "
        "WHERE name='0013_goal_discovery_candidate'"
    ).fetchone()[0]
    conn.close()
    assert value == 0
    assert applied == 1


def test_pages_redirect_to_setup_before_it_is_done(client):
    res = client.get('/')
    assert res.status_code == 302
    assert res.headers['Location'].endswith('/setup')


def test_setup_page_renders(client):
    assert client.get('/setup').status_code == 200


def test_preset_endpoint_returns_the_tree(client):
    data = client.get('/api/setup/preset/household').get_json()
    assert data['categories'] and data['income_categories']
    assert 'CHECK ME' in data['income_categories']


def test_unknown_preset_is_404(client):
    assert client.get('/api/setup/preset/nope').status_code == 404
    assert client.get('/api/setup/preset/..%2Fetc').status_code in (404, 400)


def test_setup_applies_only_what_was_kept(client, db):
    res = client.post('/api/setup', json={
        'preset': 'household',
        'categories': [{'name': 'Groceries', 'children': []},
                       {'name': 'Transport', 'children': ['Fuel']}],
        'income_categories': ['Salary', 'CHECK ME'],
        'accounts': [['Main', 'bank'], ['Wallet', 'cash']],
        'settings': {'currency': '€', 'locale': 'de-DE', 'calc.enabled': '0'},
        'calc_items': [],
    })
    assert res.status_code == 200, res.get_json()

    names = [r['name'] for r in db.execute('SELECT name FROM categories ORDER BY name')]
    assert names == ['Fuel', 'Groceries', 'Transport']
    assert [r['name'] for r in db.execute('SELECT name FROM accounts ORDER BY name')] == ['Main', 'Wallet']
    settings = dict(db.execute('SELECT key, value FROM settings').fetchall())
    assert settings['currency'] == '€'
    assert settings['locale'] == 'de-DE'


def test_setup_refuses_to_run_twice(client):
    """Applying to a configured install would skip every non-empty table and
    report success while doing nothing."""
    res = client.post('/api/setup', json={'preset': 'household', 'accounts': [['X', 'bank']]})
    assert res.status_code == 409


def test_app_is_reachable_once_set_up(client):
    assert client.get('/').status_code == 200
