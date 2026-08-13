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


def test_pages_redirect_to_setup_before_it_is_done(client):
    res = client.get('/')
    assert res.status_code == 302
    assert res.headers['Location'].endswith('/setup')


def test_setup_page_renders(client):
    assert client.get('/setup').status_code == 200


def test_preset_endpoint_returns_the_tree(client):
    data = client.get('/api/setup/preset/household-en').get_json()
    assert data['categories'] and data['income_categories']
    assert 'CHECK ME' in data['income_categories']


def test_unknown_preset_is_404(client):
    assert client.get('/api/setup/preset/nope').status_code == 404
    assert client.get('/api/setup/preset/..%2Fetc').status_code in (404, 400)


def test_setup_applies_only_what_was_kept(client, db):
    res = client.post('/api/setup', json={
        'preset': 'household-en',
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
    res = client.post('/api/setup', json={'preset': 'household-en', 'accounts': [['X', 'bank']]})
    assert res.status_code == 409


def test_app_is_reachable_once_set_up(client):
    assert client.get('/').status_code == 200
