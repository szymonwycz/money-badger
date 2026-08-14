"""Login, accounts and API-token tests.

app.py reads the password and token from the environment once, at import time.
Running the whole suite in one process means test_app.py usually imports the
module first, with auth switched off, so setting the environment here is not
enough on its own — the values are also assigned onto the module below.
"""
import os
import sqlite3
import sys
import tempfile

import pytest
from werkzeug.security import generate_password_hash

PASSWORD = 'correct horse battery staple'
ADMIN = 'admin'
TOKEN = 'test-token-value'

TEST_DB = os.path.join(tempfile.mkdtemp(prefix='money-badger-auth-'), 'test.db')
os.environ['BUDGET_DB'] = TEST_DB
os.environ['MB_PASSWORD_HASH'] = generate_password_hash(PASSWORD)
os.environ['MB_API_TOKEN'] = TOKEN
os.environ['SECRET_KEY'] = 'test-secret'
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as badger  # noqa: E402

# /transactions is a page, and pages on an unconfigured install redirect to
# /setup. Running the whole suite, test_app.py's preset has already been applied
# to this database; running this file alone, it hasn't.
_seed = sqlite3.connect(badger.DB_PATH)
if not _seed.execute('SELECT 1 FROM categories LIMIT 1').fetchone():
    _seed.execute("INSERT INTO categories (name) VALUES ('Groceries')")
    _seed.commit()
_seed.close()


@pytest.fixture()
def client():
    """Switch auth on for the duration of one test and off again afterwards.

    pytest imports every test module before running anything, so doing this at
    import time would turn auth on for test_app.py too — where every request is
    made signed out — and fail the whole suite.
    """
    badger.PASSWORD_HASH = os.environ['MB_PASSWORD_HASH']
    badger.API_TOKEN = TOKEN
    badger.app.secret_key = 'test-secret'
    badger.app.config['TESTING'] = True
    # On a real install this runs at import, where the password is already in the
    # environment. Here it isn't, so the promotion happens now — it does nothing
    # once the account exists, so every test after the first just gets its id.
    badger.LEGACY_ADMIN_ID = badger.ensure_admin_account()
    # Module-level state: without this a wrong-password test leaks its failure
    # count into the next one, and eventually locks the whole file out.
    badger._login_failures.clear()
    try:
        with badger.app.test_client() as c:
            yield c
    finally:
        badger.PASSWORD_HASH = ''
        badger.API_TOKEN = ''
        badger._login_failures.clear()


def _login(client, username=ADMIN, password=PASSWORD):
    return client.post('/login', data={'username': username, 'password': password})


def _users():
    """Straight from the database — the API never returns hashes, and one of the
    tests below is specifically about which hash ended up in there."""
    conn = sqlite3.connect(badger.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute('SELECT * FROM users ORDER BY id')]
    finally:
        conn.close()


def _id_of(username):
    return [u for u in _users() if u['username'] == username][0]['id']


def test_pages_redirect_to_login_when_signed_out(client):
    res = client.get('/')
    assert res.status_code == 302
    assert '/login' in res.headers['Location']


def test_api_returns_401_rather_than_a_redirect(client):
    """A fetch() following a redirect to an HTML login page looks like success
    to the caller and then fails to parse — say no plainly instead."""
    res = client.get('/api/budget?month=1&year=2030')
    assert res.status_code == 401
    assert res.get_json()['error']


def test_export_is_not_reachable_signed_out(client):
    """The whole ledger in one request; this is the endpoint worth checking."""
    assert client.get('/api/export/transactions.csv?year=2030').status_code == 401


def test_login_page_itself_is_reachable(client):
    assert client.get('/login').status_code == 200


def test_the_instance_password_became_an_admin_account(client):
    """Upgrading from the single-password build must ask the host for nothing:
    the hash out of .env is the admin's hash, with a username in front of it."""
    first = _users()[0]
    assert first['username'] == ADMIN
    assert first['is_admin'] == 1
    assert first['password_hash'] == os.environ['MB_PASSWORD_HASH']
    assert _login(client).status_code == 302


def test_a_session_from_the_single_password_build_survives(client):
    """Those sessions carry no user id because they predate the users table.
    Throwing them out would log the whole household out on upgrade; they belong
    to the account that password turned into."""
    with client.session_transaction() as s:
        s['authed'] = True  # exactly what the old login wrote, and nothing else
    assert client.get('/api/settings').status_code == 200
    assert client.get('/api/users').status_code == 200, 'adopted by the admin account'


def test_wrong_password_does_not_sign_in(client):
    res = _login(client, ADMIN, 'wrong')
    assert res.status_code == 401
    assert client.get('/api/settings').status_code == 401


def test_unknown_username_does_not_sign_in(client):
    res = _login(client, 'nobody', PASSWORD)
    assert res.status_code == 401
    assert client.get('/api/settings').status_code == 401


def test_correct_password_signs_in(client):
    res = _login(client)
    assert res.status_code == 302
    assert client.get('/api/settings').status_code == 200


def test_logout_ends_the_session(client):
    _login(client)
    client.get('/logout')
    assert client.get('/api/settings').status_code == 401


def test_admin_adds_a_person_who_can_then_sign_in(client):
    _login(client)
    assert client.post('/api/users',
                       json={'username': 'ola', 'password': 'secret1234'}).status_code == 200
    client.get('/logout')
    assert _login(client, 'ola', 'secret1234').status_code == 302
    assert 'ola' in [u['username'] for u in _users()]


def test_a_plain_person_cannot_manage_people(client):
    """Same budget, but adding and removing logins is the host's job."""
    _login(client)
    client.post('/api/users', json={'username': 'basic.user', 'password': 'secret1234'})
    client.get('/logout')
    _login(client, 'basic.user', 'secret1234')
    assert client.get('/api/users').status_code == 403
    assert client.post('/api/users',
                       json={'username': 'sneaky', 'password': 'secret1234'}).status_code == 403
    assert client.delete(f'/api/users/{_id_of(ADMIN)}').status_code == 403
    assert 'sneaky' not in [u['username'] for u in _users()]


def test_new_accounts_are_validated(client):
    _login(client)
    assert client.post('/api/users',
                       json={'username': 'a b', 'password': 'secret1234'}).status_code == 400
    assert client.post('/api/users',
                       json={'username': 'shorty', 'password': 'short'}).status_code == 400
    assert client.post('/api/users',
                       json={'username': ADMIN, 'password': 'secret1234'}).status_code == 409
    assert client.post('/api/users',
                       json={'username': ADMIN.upper(), 'password': 'secret1234'}).status_code == 409


def test_an_admin_cannot_remove_their_own_account(client):
    """Which is also what stops the last admin walking out of the door."""
    _login(client)
    assert client.delete(f'/api/users/{_id_of(ADMIN)}').status_code == 400


def test_removing_a_person_keeps_their_transactions(client):
    _login(client)
    client.post('/api/users', json={'username': 'leaver', 'password': 'secret1234'})
    uid = _id_of('leaver')
    client.get('/logout')
    _login(client, 'leaver', 'secret1234')
    client.post('/api/transactions',
                json={'date': '2044-03-15', 'amount': 10, 'description': 'lunch'})
    client.get('/logout')

    _login(client)
    assert client.delete(f'/api/users/{uid}').status_code == 200
    txs = client.get('/api/transactions?month=3&year=2044').get_json()
    assert [t['description'] for t in txs] == ['lunch']
    assert txs[0]['created_by'] is None, 'the money stays, the author goes'
    client.delete(f'/api/transactions/{txs[0]["id"]}')


def test_a_removed_account_loses_its_open_session(client):
    _login(client)
    client.post('/api/users', json={'username': 'evicted', 'password': 'secret1234'})
    uid = _id_of('evicted')
    client.get('/logout')

    # A second browser, left signed in. Not a `with` block: the fixture's client
    # already holds a request context open and nesting a second one breaks the
    # teardown order.
    theirs = badger.app.test_client()
    _login(theirs, 'evicted', 'secret1234')
    assert theirs.get('/api/settings').status_code == 200

    _login(client)
    client.delete(f'/api/users/{uid}')
    assert theirs.get('/api/settings').status_code == 401


def test_a_transaction_records_who_entered_it(client):
    _login(client)
    client.post('/api/transactions',
                json={'date': '2044-04-15', 'amount': 12, 'description': 'coffee'})
    txs = client.get('/api/transactions?month=4&year=2044').get_json()
    assert txs[0]['created_by'] == ADMIN
    client.delete(f'/api/transactions/{txs[0]["id"]}')


def test_a_person_changes_their_own_password(client):
    _login(client)
    client.post('/api/users', json={'username': 'pw.user', 'password': 'secret1234'})
    client.get('/logout')
    _login(client, 'pw.user', 'secret1234')

    assert client.put('/api/account/password',
                      json={'current_password': 'wrong',
                            'password': 'nowehaslo123'}).status_code == 403
    assert client.put('/api/account/password',
                      json={'current_password': 'secret1234',
                            'password': 'short'}).status_code == 400
    assert client.put('/api/account/password',
                      json={'current_password': 'secret1234',
                            'password': 'nowehaslo123'}).status_code == 200

    client.get('/logout')
    assert _login(client, 'pw.user', 'secret1234').status_code == 401
    assert _login(client, 'pw.user', 'nowehaslo123').status_code == 302


def test_an_admin_resets_a_forgotten_password(client):
    _login(client)
    client.post('/api/users', json={'username': 'forgetful', 'password': 'secret1234'})
    uid = _id_of('forgetful')
    assert client.put(f'/api/users/{uid}/password',
                      json={'password': 'resetowane12'}).status_code == 200
    # Their own still needs the current one, or an unlocked browser is a takeover.
    assert client.put(f'/api/users/{_id_of(ADMIN)}/password',
                      json={'password': 'resetowane12'}).status_code == 400
    client.get('/logout')
    assert _login(client, 'forgetful', 'resetowane12').status_code == 302


def test_the_transactions_table_shows_who_entered_a_row(client):
    """The point of `created_by`: the household can see who added what."""
    _login(client)
    client.post('/api/transactions',
                json={'date': '2044-06-15', 'amount': 8, 'description': 'author test'})
    html = client.get('/transactions').get_data(as_text=True)
    assert '>BY<' in html
    assert 'SHOW_AUTHOR = true' in html

    tx = client.get('/api/transactions?month=6&year=2044').get_json()[0]
    assert tx['created_by'] == ADMIN, 'the column is filled from here'
    client.delete(f'/api/transactions/{tx["id"]}')


def test_api_token_works_without_a_session(client):
    """This is how the sync pipeline reaches the app — it cannot log in."""
    res = client.get('/api/settings', headers={'X-MB-Token': TOKEN})
    assert res.status_code == 200


def test_wrong_api_token_is_rejected(client):
    assert client.get('/api/settings', headers={'X-MB-Token': 'nope'}).status_code == 401


def test_a_token_request_has_no_author(client):
    """The pipeline is a script, not a person."""
    hdr = {'X-MB-Token': TOKEN}
    client.post('/api/transactions', headers=hdr,
                json={'date': '2044-05-15', 'amount': 5, 'description': 'imported'})
    txs = client.get('/api/transactions?month=5&year=2044', headers=hdr).get_json()
    assert txs[0]['created_by'] is None
    client.delete(f'/api/transactions/{txs[0]["id"]}', headers=hdr)


def test_static_files_stay_public(client):
    """The login page has to be able to load its own stylesheet."""
    assert client.get('/static/style.css').status_code == 200


def test_login_redirect_target_stays_inside_the_app(client):
    """?next= is attacker-controllable, so it must never send the browser off-site."""
    res = client.post('/login?next=https://evil.example/x',
                      data={'username': ADMIN, 'password': PASSWORD})
    assert res.headers['Location'] == '/'
    res = client.post('/login?next=//evil.example/x',
                      data={'username': ADMIN, 'password': PASSWORD})
    assert res.headers['Location'] == '/'


def test_lockout_after_five_wrong_passwords(client):
    """Without this an online script can grind through a weak password."""
    for _ in range(badger.MAX_LOGIN_ATTEMPTS):
        assert _login(client, ADMIN, 'wrong').status_code == 401

    assert _login(client, ADMIN, 'wrong').status_code == 429

    # The lockout has to hold against the real password too, or an attacker
    # simply keeps guessing and the counter only ever delays the winning try.
    res = _login(client)
    assert res.status_code == 429
    assert client.get('/api/settings').status_code == 401


def test_the_lockout_counts_across_usernames(client):
    """Guessing a name and guessing a password are the same attack from here."""
    for i in range(badger.MAX_LOGIN_ATTEMPTS):
        assert _login(client, f'guess{i}', 'wrong').status_code == 401
    assert _login(client).status_code == 429


def test_signing_in_clears_the_failure_count(client):
    """Four fat-fingered attempts must not leave you one typo from a lockout."""
    for _ in range(badger.MAX_LOGIN_ATTEMPTS - 1):
        _login(client, ADMIN, 'wrong')
    assert _login(client).status_code == 302

    client.get('/logout')
    for _ in range(badger.MAX_LOGIN_ATTEMPTS - 1):
        assert _login(client, ADMIN, 'wrong').status_code == 401


def test_forwarded_for_cannot_be_rotated_to_dodge_the_lockout(client):
    """nginx appends to X-Forwarded-For, so the first entry is client-supplied.
    Reading that one would let an attacker send a fresh fake IP every request and
    never accumulate a count."""
    for i in range(badger.MAX_LOGIN_ATTEMPTS):
        res = client.post('/login', data={'username': ADMIN, 'password': 'wrong'},
                          headers={'X-Forwarded-For': f'10.0.0.{i}, 192.168.1.50'})
        assert res.status_code == 401

    res = client.post('/login', data={'username': ADMIN, 'password': 'wrong'},
                      headers={'X-Forwarded-For': '10.0.0.99, 192.168.1.50'})
    assert res.status_code == 429, 'all six came through the same proxy hop'


def test_security_headers_are_present(client):
    res = client.get('/login')
    assert res.headers['X-Content-Type-Options'] == 'nosniff'
    assert res.headers['X-Frame-Options'] == 'DENY'
    assert "frame-ancestors 'none'" in res.headers['Content-Security-Policy']
