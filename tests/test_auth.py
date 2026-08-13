"""Login and API-token tests.

app.py reads the password and token from the environment once, at import time.
Running the whole suite in one process means test_app.py usually imports the
module first, with auth switched off, so setting the environment here is not
enough on its own — the values are also assigned onto the module below.
"""
import os
import sys
import tempfile

import pytest
from werkzeug.security import generate_password_hash

PASSWORD = 'correct horse battery staple'
TOKEN = 'test-token-value'

TEST_DB = os.path.join(tempfile.mkdtemp(prefix='money-badger-auth-'), 'test.db')
os.environ['BUDGET_DB'] = TEST_DB
os.environ['MB_PASSWORD_HASH'] = generate_password_hash(PASSWORD)
os.environ['MB_API_TOKEN'] = TOKEN
os.environ['SECRET_KEY'] = 'test-secret'
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as badger  # noqa: E402


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


def test_wrong_password_does_not_sign_in(client):
    res = client.post('/login', data={'password': 'wrong'})
    assert res.status_code == 401
    assert client.get('/api/settings').status_code == 401


def test_correct_password_signs_in(client):
    res = client.post('/login', data={'password': PASSWORD})
    assert res.status_code == 302
    assert client.get('/api/settings').status_code == 200


def test_logout_ends_the_session(client):
    client.post('/login', data={'password': PASSWORD})
    client.get('/logout')
    assert client.get('/api/settings').status_code == 401


def test_api_token_works_without_a_session(client):
    """This is how the sync pipeline reaches the app — it cannot log in."""
    res = client.get('/api/settings', headers={'X-MB-Token': TOKEN})
    assert res.status_code == 200


def test_wrong_api_token_is_rejected(client):
    assert client.get('/api/settings', headers={'X-MB-Token': 'nope'}).status_code == 401


def test_static_files_stay_public(client):
    """The login page has to be able to load its own stylesheet."""
    assert client.get('/static/style.css').status_code == 200


def test_login_redirect_target_stays_inside_the_app(client):
    """?next= is attacker-controllable, so it must never send the browser off-site."""
    res = client.post('/login?next=https://evil.example/x', data={'password': PASSWORD})
    assert res.headers['Location'] == '/'
    res = client.post('/login?next=//evil.example/x', data={'password': PASSWORD})
    assert res.headers['Location'] == '/'


def test_lockout_after_five_wrong_passwords(client):
    """Without this an online script can grind through a weak password."""
    for _ in range(badger.MAX_LOGIN_ATTEMPTS):
        assert client.post('/login', data={'password': 'wrong'}).status_code == 401

    res = client.post('/login', data={'password': 'wrong'})
    assert res.status_code == 429

    # The lockout has to hold against the real password too, or an attacker
    # simply keeps guessing and the counter only ever delays the winning try.
    res = client.post('/login', data={'password': PASSWORD})
    assert res.status_code == 429
    assert client.get('/api/settings').status_code == 401


def test_signing_in_clears_the_failure_count(client):
    """Four fat-fingered attempts must not leave you one typo from a lockout."""
    for _ in range(badger.MAX_LOGIN_ATTEMPTS - 1):
        client.post('/login', data={'password': 'wrong'})
    assert client.post('/login', data={'password': PASSWORD}).status_code == 302

    client.get('/logout')
    for _ in range(badger.MAX_LOGIN_ATTEMPTS - 1):
        assert client.post('/login', data={'password': 'wrong'}).status_code == 401


def test_forwarded_for_cannot_be_rotated_to_dodge_the_lockout(client):
    """nginx appends to X-Forwarded-For, so the first entry is client-supplied.
    Reading that one would let an attacker send a fresh fake IP every request and
    never accumulate a count."""
    for i in range(badger.MAX_LOGIN_ATTEMPTS):
        res = client.post('/login', data={'password': 'wrong'},
                          headers={'X-Forwarded-For': f'10.0.0.{i}, 192.168.1.50'})
        assert res.status_code == 401

    res = client.post('/login', data={'password': 'wrong'},
                      headers={'X-Forwarded-For': '10.0.0.99, 192.168.1.50'})
    assert res.status_code == 429, 'all six came through the same proxy hop'


def test_security_headers_are_present(client):
    res = client.get('/login')
    assert res.headers['X-Content-Type-Options'] == 'nosniff'
    assert res.headers['X-Frame-Options'] == 'DENY'
    assert "frame-ancestors 'none'" in res.headers['Content-Security-Policy']
