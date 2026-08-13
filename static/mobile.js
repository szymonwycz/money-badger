// Money Badger — mobile PWA view. Same API and database as the desktop app.
const state = {
  month: INIT_MONTH,
  year:  INIT_YEAR,
  categories: [],
  accounts: [],
  budget: { additionals: {}, spent: {}, income: 0 },
  expanded: new Set(),
  txFilterCategoryId: null,
  check: { expenses: [], income: [] },
};

// ── offline write queue ──────────────────────────────────────────────────────
// ponytail: two people, two phones, each with its own local queue — new
// transactions (add/edit/delete) are commutative, so a plain FIFO per device
// is enough: whoever's back online first just replays their own writes, no
// merging needed. A write the server actually rejects (validation error, not
// a network failure) is dropped after one attempt rather than retried forever.
// EXCEPTION: account balance correction (below, `noQueue: true`) is NOT queued
// — the server computes it as a diff against the *current* live balance, so
// replaying it late (after the other person's transactions landed) would
// silently miscalculate. That one fails fast instead.
const QUEUE_KEY  = 'mb-offline-queue';
const FAILED_KEY = 'mb-failed-writes';

function loadQueue() {
  try { return JSON.parse(localStorage.getItem(QUEUE_KEY) || '[]'); } catch { return []; }
}
function saveQueue(q) {
  localStorage.setItem(QUEUE_KEY, JSON.stringify(q));
  renderQueueBadge(q.length);
}
function loadFailed() {
  try { return JSON.parse(localStorage.getItem(FAILED_KEY) || '[]'); } catch { return []; }
}
/* A queued write the server later rejects used to be dropped with a console.warn,
   after the user had already been told "✓ Queued 250 zł — will sync". Keep it and
   say so — silently losing a recorded expense is the one thing this app must not do. */
function saveFailed(f) {
  localStorage.setItem(FAILED_KEY, JSON.stringify(f));
  renderQueueBadge(loadQueue().length);
}
function renderQueueBadge(pending) {
  const badge  = document.getElementById('m-queue-badge');
  if (!badge) return;
  const failed = loadFailed().length;
  if (failed) {
    badge.textContent = `${failed} FAILED — TAP`;
    badge.className   = 'm-queue-badge failed';
    badge.style.display = '';
    badge.onclick = showFailedWrites;
  } else if (pending) {
    badge.textContent = `${pending} QUEUED`;
    badge.className   = 'm-queue-badge';
    badge.style.display = '';
    badge.onclick = null;
  } else {
    badge.style.display = 'none';
    badge.onclick = null;
  }
}
function showFailedWrites() {
  const f = loadFailed();
  if (!f.length) return;
  const lines = f.map(x => `• ${x.method} ${x.url}\n  ${x.reason}\n  ${x.body || ''}`).join('\n\n');
  if (confirm(`${f.length} write(s) the server rejected:\n\n${lines}\n\nClear this list?`)) {
    saveFailed([]);
  }
}
function queueWrite(url, opts) {
  const q = loadQueue();
  q.push({ url, method: opts.method || 'POST', body: opts.body || null });
  saveQueue(q);
  return { ok: true, queued: true };
}

/* flushQueue is wired to both `online` and `visibilitychange`. On iOS, resuming a
   backgrounded PWA on a fresh network fires both in the same tick — two concurrent
   runs each read their own copy of the queue and POSTed the same expense twice. */
let flushing = false;
async function flushQueue() {
  if (flushing) return;
  flushing = true;
  try {
    let q = loadQueue();
    while (q.length) {
      const item = q[0];
      let res;
      try {
        res = await fetch(item.url, {
          method: item.method,
          headers: { 'Content-Type': 'application/json' },
          body: item.body,
        });
      } catch (err) {
        return; // still offline — stop, keep remaining items queued in order
      }
      if (!res.ok) {
        const body = await res.json().catch(() => null);
        const failed = loadFailed();
        failed.push({ ...item, reason: (body && body.error) || `HTTP ${res.status}`,
                      at: new Date().toISOString() });
        saveFailed(failed);
      }
      q = loadQueue();
      q.shift();
      saveQueue(q);
    }
  } finally {
    flushing = false;
  }
  loadAccounts(); loadBudget().then(() => { if (document.getElementById('panel-budget').classList.contains('active')) loadBudgetTab(); });
  if (document.getElementById('panel-txns').classList.contains('active')) loadTxTab();
}
window.addEventListener('online', flushQueue);
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') flushQueue();
});

/* Overrides the shared api() from common.js: on mobile a failed write goes to the
   offline queue instead of surfacing an error. Reads use the shared behaviour.
   Only a genuine fetch rejection queues — a server response that happens not to be
   JSON must not, or a write the server already committed gets replayed as a duplicate. */
function api(url, opts) {
  const { noQueue, ...fetchOpts } = opts || {};
  const isWrite = !!(fetchOpts.method && fetchOpts.method !== 'GET');
  if (!isWrite) return apiFetch(url, fetchOpts);

  return fetch(url, { headers: {'Content-Type': 'application/json'}, ...fetchOpts })
    .then(async r => {
      const body = await r.json().catch(() => null);
      if (!r.ok || (body && body.error)) {
        const msg = (body && body.error) || `Server error (${r.status})`;
        toast(msg, 'error');
        return { error: msg };
      }
      return body;
    })
    .catch(() => noQueue ? { error: 'offline' } : queueWrite(url, fetchOpts));
}

// END OFFLINE QUEUE — tests/test_offline_queue.mjs evaluates everything above
// this line in isolation. Keep DOM wiring below it.

// ── tabs ─────────────────────────────────────────────────────────────────────

document.querySelectorAll('.m-tab').forEach(tab => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.m-tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.m-panel').forEach(p => p.classList.remove('active'));
    document.getElementById('m-tx-detail')?.classList.remove('open');
    tab.classList.add('active');
    document.getElementById(tab.dataset.panel).classList.add('active');
    if (tab.dataset.panel === 'panel-balances') loadAccounts();
    if (tab.dataset.panel === 'panel-budget') loadBudgetTab();
    if (tab.dataset.panel === 'panel-txns') { clearTxFilter(); loadTxTab(); }
    if (tab.dataset.panel === 'panel-check') loadCheckTab();
  });
});

// ── data ─────────────────────────────────────────────────────────────────────

async function loadCategories() {
  state.categories = await api(`/api/categories?month=${state.month}&year=${state.year}`);
}

async function loadAccounts() {
  state.accounts = await api('/api/accounts');
  renderBalances();
  renderAccountSelect();
}

async function loadIncomeCategories() {
  state.incomeCategories = await api('/api/income-categories');
  renderIncomeCategorySelect();
}

async function loadBudget() {
  const d = await api(`/api/budget?month=${state.month}&year=${state.year}`);
  state.budget = { additionals: d.additionals || {}, spent: d.spent || {}, income: d.income || 0 };
}

// ── ADD EXPENSE ──────────────────────────────────────────────────────────────

function renderCategorySelect() {
  const sel = document.getElementById('m-category');
  sel.innerHTML = '<option value="">—</option>';
  for (const g of state.categories) {
    if (g.children.length) {
      const grp = document.createElement('optgroup');
      grp.label = g.name;
      for (const c of g.children) {
        grp.innerHTML += `<option value="${c.id}">${esc(c.name)}</option>`;
      }
      sel.appendChild(grp);
    } else {
      sel.innerHTML += `<option value="${g.id}">${esc(g.name)}</option>`;
    }
  }
  const last = localStorage.getItem('mb-last-category');
  if (last && sel.querySelector(`option[value="${last}"]`)) sel.value = last;
}

function renderIncomeCategorySelect() {
  const sel = document.getElementById('m-income-category');
  sel.innerHTML = '<option value="">—</option>';
  for (const c of state.incomeCategories || []) {
    sel.innerHTML += `<option value="${c.id}">${esc(c.name)}</option>`;
  }
  const last = localStorage.getItem('mb-last-income-category');
  if (last && sel.querySelector(`option[value="${last}"]`)) sel.value = last;
}

function renderAccountSelect() {
  const sel = document.getElementById('m-account');
  sel.innerHTML = '';
  for (const a of state.accounts.filter(a => a.type !== 'asset')) {
    sel.innerHTML += `<option value="${esc(a.name)}">${esc(a.name)}</option>`;
  }
  // Most on-the-go entries are cash, so default there until the user picks otherwise.
  const cash = state.accounts.find(a => a.type === 'cash');
  const last = localStorage.getItem('mb-last-account') || (cash && cash.name);
  if ([...sel.options].some(o => o.value === last)) sel.value = last;
  // transfers can also target asset accounts (e.g. moving money to savings)
  const toSel = document.getElementById('m-account-to');
  toSel.innerHTML = '';
  for (const a of state.accounts) {
    toSel.innerHTML += `<option value="${esc(a.name)}">${esc(a.name)}</option>`;
  }
}

// ── EXPENSE / TRANSFER toggle ────────────────────────────────────────────────

state.addType = 'Expense';

function setAddType(type) {
  state.addType = type;
  const isTransfer = type === 'Money Transfer';
  const isIncome   = type === 'Income';
  document.getElementById('m-type-expense').classList.toggle('active', !isTransfer && !isIncome);
  document.getElementById('m-type-income').classList.toggle('active', isIncome);
  document.getElementById('m-type-transfer').classList.toggle('active', isTransfer);
  document.getElementById('m-category-field').style.display = (isTransfer || isIncome) ? 'none' : '';
  document.getElementById('m-income-category-field').style.display = isIncome ? '' : 'none';
  document.getElementById('m-account-to-field').style.display = isTransfer ? '' : 'none';
  document.getElementById('m-account-label').textContent = isTransfer ? 'FROM ACCOUNT' : 'ACCOUNT';
  document.getElementById('m-add-btn').textContent =
    isTransfer ? 'ADD TRANSFER' : isIncome ? 'ADD INCOME' : 'ADD EXPENSE';
}

document.getElementById('m-type-expense').addEventListener('click', () => setAddType('Expense'));
document.getElementById('m-type-income').addEventListener('click', () => setAddType('Income'));
document.getElementById('m-type-transfer').addEventListener('click', () => setAddType('Money Transfer'));

function todayISO() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
}

document.getElementById('m-add-btn').addEventListener('click', async () => {
  const btn    = document.getElementById('m-add-btn');
  const status = document.getElementById('m-add-status');
  const amount   = parseFloat(document.getElementById('m-amount').value.replace(',', '.'));
  const catId    = parseInt(document.getElementById('m-category').value) || null;
  const incCatId = parseInt(document.getElementById('m-income-category').value) || null;
  const acct     = document.getElementById('m-account').value;
  const desc     = document.getElementById('m-desc').value.trim();
  const date     = document.getElementById('m-date').value || todayISO();

  if (!amount || amount <= 0) {
    status.textContent = 'Enter an amount';
    status.style.color = 'var(--red)';
    return;
  }

  const isTransfer = state.addType === 'Money Transfer';
  const isIncome   = state.addType === 'Income';
  if (isIncome && !incCatId) {
    status.textContent = 'Pick an income category';
    status.style.color = 'var(--red)';
    return;
  }
  const acctTo = document.getElementById('m-account-to').value;
  if (isTransfer && acct === acctTo) {
    status.textContent = 'Pick two different accounts';
    status.style.color = 'var(--red)';
    return;
  }

  btn.disabled = true;
  try {
    const payload = isTransfer
      ? { date, amount, account: acct, account_to: acctTo, description: desc, tx_type: 'Money Transfer' }
      : isIncome
      ? { date, amount, account: acct, description: desc, tx_type: 'Income', income_category_id: incCatId }
      : { date, amount, account: acct, description: desc, category_id: catId };
    const res = await api('/api/transactions', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    if (res.error) throw new Error(res.error);
    status.textContent = res.queued ? `✓ Queued ${fmtNum(amount)} — will sync` : `✓ Added ${fmtNum(amount)}`;
    status.style.color = res.queued ? 'var(--yellow)' : 'var(--green)';
    document.getElementById('m-amount').value = '';
    document.getElementById('m-desc').value = '';
    if (!isTransfer && !isIncome && catId) localStorage.setItem('mb-last-category', catId);
    if (isIncome && incCatId) localStorage.setItem('mb-last-income-category', incCatId);
    if (!isTransfer) localStorage.setItem('mb-last-account', acct);
    setTimeout(() => { status.textContent = ''; }, 2500);
  } catch (err) {
    status.textContent = 'Failed — try again';
    status.style.color = 'var(--red)';
  }
  btn.disabled = false;
});

// ── BALANCES ─────────────────────────────────────────────────────────────────

function renderBalances() {
  const accEl   = document.getElementById('m-bal-accounts');
  const assetEl = document.getElementById('m-bal-assets');
  accEl.innerHTML = ''; assetEl.innerHTML = '';
  let accTotal = 0, assetTotal = 0;

  for (const a of state.accounts) {
    const isAsset = a.type === 'asset';
    const color   = a.balance < 0 ? 'var(--red)' : (isAsset ? 'var(--teal)' : 'var(--green)');
    const row = document.createElement('div');
    row.className = 'm-bal-row';
    row.innerHTML = `<span>${esc(a.name)}</span><span class="val" style="color:${color}">${fmtNum(a.balance)}</span>`;
    row.addEventListener('click', async () => {
      const input = prompt(`New balance for ${a.name}:`, a.balance);
      if (input === null) return;
      const balance = parseFloat(String(input).replace(',', '.'));
      if (isNaN(balance)) return;
      const res = await api(`/api/accounts/${a.id}`,
        { method: 'PUT', body: JSON.stringify({ balance }), noQueue: true });
      if (res && res.error === 'offline') {
        alert('No connection — balance correction depends on the live total, so it can’t be queued. Reconnect and try again.');
        return;
      }
      loadAccounts();
    });
    (isAsset ? assetEl : accEl).appendChild(row);
    if (isAsset) assetTotal += a.balance; else accTotal += a.balance;
  }
  document.getElementById('m-bal-accounts-total').textContent = fmtNum(accTotal);
  document.getElementById('m-bal-assets-total').textContent   = fmtNum(assetTotal);
}

// ── TRANSACTIONS ─────────────────────────────────────────────────────────────

state.txs = [];
state.openTx = null;

function txColor(tx) {
  if (tx.tx_type === 'Money Transfer') return 'var(--blue)';
  if (tx.income_category_id || tx.tx_type === 'Income') return 'var(--green)';
  if (tx.tx_type === 'Balance Adjust') return tx.amount > 0 ? 'var(--green)' : 'var(--yellow)';
  return 'var(--yellow)';
}

function fmtDayMonth(iso) {
  const [, m, d] = iso.substring(0, 10).split('-');
  return `${d}/${m}`;
}

function clearTxFilter() {
  state.txFilterCategoryId = null;
  document.getElementById('m-tx-filter').style.display = 'none';
}

// Drill-down entry point from the budget tab: jump to the txns tab pre-filtered
// to one category so "why is this over budget" has an answer.
function openCategoryTx(catId, catName) {
  state.txFilterCategoryId = catId;
  document.getElementById('m-tx-filter-name').textContent = catName;
  document.getElementById('m-tx-filter').style.display = '';
  document.querySelectorAll('.m-tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.m-panel').forEach(p => p.classList.remove('active'));
  document.getElementById('m-tx-detail')?.classList.remove('open');
  document.querySelector('.m-tab[data-panel="panel-txns"]').classList.add('active');
  document.getElementById('panel-txns').classList.add('active');
  loadTxTab();
}
document.getElementById('m-tx-filter-clear').addEventListener('click', () => {
  clearTxFilter();
  loadTxTab();
});

async function loadTxTab() {
  document.getElementById('m-tx-month-label').textContent =
    `${MONTHS[state.month-1].toUpperCase()} ${state.year}`;
  const catQ = state.txFilterCategoryId ? `&category_id=${state.txFilterCategoryId}` : '';
  state.txs = await api(`/api/transactions?month=${state.month}&year=${state.year}${catQ}`);
  renderTxList();
}

/* Mobile had month navigation only — finding one specific expense on a phone meant
   scrolling the whole month. Filters the already-fetched array, no extra request. */
function filteredTxs() {
  const q = (document.getElementById('m-tx-search')?.value || '').trim().toLowerCase();
  if (!q) return state.txs;
  const amountQ = parseFloat(q.replace(',', '.'));
  return state.txs.filter(t => {
    if ((t.description || '').toLowerCase().includes(q)) return true;
    if ((t.account || '').toLowerCase().includes(q)) return true;
    if (!isNaN(amountQ)) {
      const a = Math.abs(t.amount);
      if (Math.abs(a - amountQ) < 0.01 || String(a).startsWith(String(amountQ))) return true;
    }
    return false;
  });
}

function renderTxList() {
  const el = document.getElementById('m-tx-list');
  el.innerHTML = '';
  const txs = filteredTxs();

  const countEl = document.getElementById('m-tx-count');
  if (countEl) countEl.textContent = txs.length === state.txs.length
    ? '' : `${txs.length} of ${state.txs.length}`;

  if (!txs.length) {
    el.innerHTML = state.txs.length
      ? '<div class="m-tx-empty">Nothing matches that search.</div>'
      : '<div class="m-tx-empty">No transactions this month.</div>';
    return;
  }
  for (const tx of txs) {
    const row = document.createElement('div');
    row.className = 'm-tx-row';
    row.innerHTML = `
      <span class="date">${fmtDayMonth(tx.date)}</span>
      <span class="amount" style="color:${txColor(tx)}">${fmtNum(Math.abs(tx.amount))}</span>
      <span class="acct">${esc(tx.account || '—')}</span>
    `;
    row.addEventListener('click', () => openTxDetail(tx));
    el.appendChild(row);
  }
}

document.getElementById('m-tx-search')?.addEventListener('input', renderTxList);

// ── CHECK ME ─────────────────────────────────────────────────────────────────
// The nightly bank sync leaves rows it couldn't categorize. They're excluded from
// every category total until someone files them, and this queue only existed on the
// desktop — so from a phone you couldn't even see that anything was waiting.

async function loadCheckTab() {
  const data = await api('/api/transactions/unreviewed');
  state.check = data;
  renderCheckList();
}

async function refreshCheckBadge() {
  const badge = document.getElementById('m-check-badge');
  if (!badge) return;
  try {
    const { count } = await apiFetch('/api/transactions/unreviewed/count');
    badge.textContent = count;
    badge.style.display = count ? '' : 'none';
  } catch { /* offline — leave the badge as it was */ }
}

function checkItem(tx, isIncome) {
  const item = document.createElement('div');
  item.className = 'm-check-item';

  let opts = '<option value="">— pick a category —</option>';
  if (isIncome) {
    for (const c of state.incomeCategories || []) {
      opts += `<option value="${c.id}">${esc(c.name)}</option>`;
    }
  } else {
    for (const g of state.categories) {
      if (g.children.length) {
        opts += `<optgroup label="${esc(g.name)}">`;
        for (const c of g.children) opts += `<option value="${c.id}">${esc(c.name)}</option>`;
        opts += '</optgroup>';
      } else {
        opts += `<option value="${g.id}">${esc(g.name)}</option>`;
      }
    }
  }

  item.innerHTML = `
    <div class="m-check-top">
      <span>${fmtDayMonth(tx.date)} · ${esc(tx.account || '—')}</span>
      <span class="amount" style="color:${txColor(tx)}">${fmtNum(Math.abs(tx.amount))}</span>
    </div>
    <div class="m-check-desc">${esc(tx.description || '—')}</div>
    <select aria-label="Category for this transaction">${opts}</select>
    <div class="m-check-actions"><button type="button" class="m-check-ignore">IGNORE</button></div>
  `;

  item.querySelector('select').addEventListener('change', async e => {
    const id = parseInt(e.target.value);
    if (!id) return;
    const url = isIncome
      ? `/api/transactions/${tx.id}/income-category`
      : `/api/transactions/${tx.id}/category`;
    const body = isIncome ? { income_category_id: id } : { category_id: id };
    const res = await api(url, { method: 'PUT', body: JSON.stringify(body) });
    if (res && res.error) return;
    toast('Filed');
    await loadCheckTab();
    refreshCheckBadge();
    loadBudget();
  });

  item.querySelector('.m-check-ignore').addEventListener('click', async () => {
    if (!confirm('Dismiss this transaction? It stays uncategorized and there is no undo here.')) return;
    const res = await api(`/api/transactions/${tx.id}/ignore`, { method: 'PUT' });
    if (res && res.error) return;
    await loadCheckTab();
    refreshCheckBadge();
  });

  return item;
}

function renderCheckList() {
  const el = document.getElementById('m-check-list');
  const countEl = document.getElementById('m-check-count');
  el.innerHTML = '';
  const { expenses = [], income = [] } = state.check || {};
  const total = expenses.length + income.length;
  countEl.textContent = total ? `${total} waiting to be categorized` : 'Nothing to review ✓';

  for (const tx of expenses) el.appendChild(checkItem(tx, false));
  for (const tx of income)   el.appendChild(checkItem(tx, true));
}

// ── transaction detail sheet ─────────────────────────────────────────────────

function fillTxCategorySelects(tx) {
  const catSel = document.getElementById('m-tx-cat');
  const subSel = document.getElementById('m-tx-subcat');
  catSel.innerHTML = '<option value="">—</option>';
  for (const g of state.categories) {
    catSel.innerHTML += `<option value="${g.id}">${esc(g.name)}</option>`;
  }
  // resolve current parent/child from tx.category_id
  let parentId = null, childId = null;
  if (tx.category_id) {
    const group = state.categories.find(g =>
      g.id === tx.category_id || g.children.some(c => c.id === tx.category_id));
    if (group) {
      parentId = group.id;
      childId  = group.id === tx.category_id ? null : tx.category_id;
    }
  }
  if (parentId) catSel.value = parentId;
  fillTxSubSelect(parentId, childId);
}

function fillTxSubSelect(parentId, childId) {
  const subSel = document.getElementById('m-tx-subcat');
  const group  = state.categories.find(g => g.id === parentId);
  subSel.innerHTML = '<option value="">—</option>';
  for (const c of (group?.children || [])) {
    subSel.innerHTML += `<option value="${c.id}">${esc(c.name)}</option>`;
  }
  if (childId) subSel.value = childId;
}

async function fillTxIncomeSelect(tx) {
  const sel = document.getElementById('m-tx-inccat');
  const cats = await api('/api/income-categories');
  sel.innerHTML = '<option value="">—</option>';
  for (const c of cats) sel.innerHTML += `<option value="${c.id}">${esc(c.name)}</option>`;
  if (tx.income_category_id) sel.value = tx.income_category_id;
}

function openTxDetail(tx) {
  state.openTx = tx;
  const isTransfer = tx.tx_type === 'Money Transfer';
  const isAdjust   = tx.tx_type === 'Balance Adjust';
  const isIncome   = !!tx.income_category_id || tx.tx_type === 'Income';

  document.getElementById('m-tx-type').textContent = (tx.tx_type || 'Expense').toUpperCase();
  document.getElementById('m-tx-status').textContent = '';
  document.getElementById('m-tx-date').value = tx.date.substring(0, 10);
  const amountEl = document.getElementById('m-tx-amount');
  amountEl.value = fmtAmountInput(Math.abs(tx.amount));
  amountEl.readOnly = isAdjust;  // adjust amounts are audit values, same rule as desktop
  const descEl = document.getElementById('m-tx-desc');
  descEl.value = tx.description || '';
  // transfer descriptions are rendered ("→ Account") and adjust ones are auto-generated —
  // saving them back would overwrite the real data
  descEl.readOnly = isTransfer || isAdjust;

  const acctSel = document.getElementById('m-tx-account');
  acctSel.innerHTML = '<option value="">—</option>';
  for (const a of state.accounts) {
    acctSel.innerHTML += `<option value="${esc(a.name)}">${esc(a.name)}</option>`;
  }
  acctSel.value = tx.account || '';

  // expense categories vs income category vs none (transfer/adjust)
  document.getElementById('m-tx-cat-field').style.display    = (!isIncome && !isTransfer && !isAdjust) ? '' : 'none';
  document.getElementById('m-tx-subcat-field').style.display = (!isIncome && !isTransfer && !isAdjust) ? '' : 'none';
  document.getElementById('m-tx-inccat-field').style.display = isIncome ? '' : 'none';
  if (isIncome) fillTxIncomeSelect(tx);
  else if (!isTransfer && !isAdjust) fillTxCategorySelects(tx);

  // swap panels in-flow (no fixed overlay — iOS misaligns taps on those with the keyboard up)
  document.getElementById('panel-txns').classList.remove('active');
  document.getElementById('m-tx-detail').classList.add('open');
  window.scrollTo(0, 0);
  document.querySelector('.m-content').scrollTop = 0;
}

function closeTxDetail() {
  document.getElementById('m-tx-detail').classList.remove('open');
  document.getElementById('panel-txns').classList.add('active');
  state.openTx = null;
  loadTxTab();
}

function flashTxSaved(ok, msg, queued) {
  const el = document.getElementById('m-tx-status');
  el.textContent = ok ? (queued ? '✓ queued — will sync' : '✓ saved') : (msg || 'error');
  el.style.color = ok ? (queued ? 'var(--yellow)' : 'var(--green)') : 'var(--red)';
  if (ok) setTimeout(() => { el.textContent = ''; }, 1800);
}

async function saveTxField(field, value) {
  const res = await api(`/api/transactions/${state.openTx.id}`, {
    method: 'PUT', body: JSON.stringify({ [field]: value }),
  });
  flashTxSaved(!res.error, res.error, res.queued);
  if (!res.error) state.openTx[field] = value;
}

document.getElementById('m-tx-back').addEventListener('click', closeTxDetail);

document.getElementById('m-tx-date').addEventListener('change', e => {
  if (e.target.value) saveTxField('date', e.target.value);
});
document.getElementById('m-tx-amount').addEventListener('blur', e => {
  if (e.target.readOnly) return;
  const v = parseFloat(String(e.target.value).replace(',', '.'));
  if (!isNaN(v) && v > 0) saveTxField('amount', v);
});
document.getElementById('m-tx-account').addEventListener('change', e => saveTxField('account', e.target.value));
document.getElementById('m-tx-desc').addEventListener('blur', e => {
  if (!e.target.readOnly) saveTxField('description', e.target.value);
});

document.getElementById('m-tx-cat').addEventListener('change', e => {
  const pid = parseInt(e.target.value) || null;
  fillTxSubSelect(pid, null);
  const group = state.categories.find(g => g.id === pid);
  // groups without children save immediately; with children we wait for the subcategory
  if (!group || !group.children.length) saveTxField('category_id', pid);
});
document.getElementById('m-tx-subcat').addEventListener('change', e => {
  const cid = parseInt(e.target.value) || parseInt(document.getElementById('m-tx-cat').value) || null;
  saveTxField('category_id', cid);
});
document.getElementById('m-tx-inccat').addEventListener('change', async e => {
  const cid = parseInt(e.target.value) || null;
  if (!cid) return;
  const res = await api(`/api/transactions/${state.openTx.id}/income-category`, {
    method: 'PUT', body: JSON.stringify({ income_category_id: cid }),
  });
  flashTxSaved(!res.error, res.error);
});
document.getElementById('m-tx-delete').addEventListener('click', async () => {
  const tx = state.openTx;
  if (!confirm(`Delete this transaction (${fmtNum(Math.abs(tx.amount))})?`)) return;
  await api(`/api/transactions/${tx.id}`, { method: 'DELETE' });
  closeTxDetail();
});

document.getElementById('m-tx-prev').addEventListener('click', () => {
  state.month--; if (state.month < 1) { state.month = 12; state.year--; }
  loadTxTab();
});
document.getElementById('m-tx-next').addEventListener('click', () => {
  state.month++; if (state.month > 12) { state.month = 1; state.year++; }
  loadTxTab();
});

// ── BUDGET ───────────────────────────────────────────────────────────────────

function catTotal(cat) {
  const adds = (state.budget.additionals[cat.id] || []).reduce((s, a) => s + a.amount, 0);
  return (cat.basic || 0) + adds;
}
function catSpent(id) {
  return Math.abs(state.budget.spent[id] || 0);
}

function renderBudget() {
  document.getElementById('m-month-label').textContent =
    `${MONTHS[state.month-1].toUpperCase()} ${state.year}`;

  let totalPlanned = 0, totalSpent = 0;
  const list = document.getElementById('m-cat-list');
  list.innerHTML = '';

  for (const g of state.categories) {
    const leaves = g.children.length ? g.children : [g];
    let gTotal = 0, gSpent = 0;
    for (const c of leaves) { gTotal += catTotal(c); gSpent += catSpent(c.id); }
    if (g.children.length) gSpent += catSpent(g.id);
    totalPlanned += gTotal; totalSpent += gSpent;

    if (gTotal === 0 && gSpent === 0) continue;  // hide empty categories on mobile

    const rem  = gTotal - gSpent;
    const pct  = gTotal > 0 ? Math.min(100, Math.round(gSpent / gTotal * 100)) : 100;
    const over = rem < 0;

    const div = document.createElement('div');
    div.className = 'm-cat';
    div.innerHTML = `
      <div class="m-cat-top">
        <span>${esc(g.name)}</span>
        <span class="rem" style="color:${over ? 'var(--red)' : 'var(--green)'}">${fmtNum(rem)}</span>
      </div>
      <div class="m-cat-bar"><div class="m-cat-fill${over ? ' over' : ''}" style="width:${pct}%"></div></div>
      <div class="m-cat-sub">${fmtNum(gSpent)} of ${fmtNum(gTotal)}</div>
      ${g.children.length ? `<div class="m-cat-children">${g.children.map(c => {
        const t = catTotal(c), s = catSpent(c.id);
        if (t === 0 && s === 0) return '';
        const r = t - s;
        return `<div class="m-cat-child" data-catid="${c.id}" data-catname="${esc(c.name)}"><span>${esc(c.name)}</span>
                <span style="color:${r < 0 ? 'var(--red)' : 'var(--text2)'}">${fmtNum(r)}</span></div>`;
      }).join('')}</div>` : ''}
    `;
    // leaf categories (no children) drill down straight to their own transactions;
    // parent categories toggle open first — the child rows drill down from there
    if (g.children.length) {
      div.addEventListener('click', e => {
        const child = e.target.closest('.m-cat-child');
        if (child) { e.stopPropagation(); openCategoryTx(parseInt(child.dataset.catid), child.dataset.catname); return; }
        div.classList.toggle('expanded');
      });
    } else {
      div.addEventListener('click', () => openCategoryTx(g.id, g.name));
    }
    list.appendChild(div);
  }

  const income = state.budget.income || 0;
  document.getElementById('m-sum-income').textContent  = income > 0 ? fmtNum(income) : '—';
  document.getElementById('m-sum-planned').textContent = fmtNum(totalPlanned);
  document.getElementById('m-sum-spent').textContent   = fmtNum(totalSpent);
  const left = income > 0 ? income - totalSpent : totalPlanned - totalSpent;
  const leftEl = document.getElementById('m-sum-left');
  leftEl.textContent = fmtNum(left);
  leftEl.style.color = left < 0 ? 'var(--red)' : 'var(--green)';
}

async function loadBudgetTab() {
  await Promise.all([loadCategories(), loadBudget()]);
  renderBudget();
}

document.getElementById('m-prev').addEventListener('click', () => {
  state.month--; if (state.month < 1) { state.month = 12; state.year--; }
  loadBudgetTab();
});
document.getElementById('m-next').addEventListener('click', () => {
  state.month++; if (state.month > 12) { state.month = 1; state.year++; }
  loadBudgetTab();
});

// ── init ─────────────────────────────────────────────────────────────────────

if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js');

document.getElementById('m-date').value = todayISO();

(async function init() {
  saveQueue(loadQueue());
  await Promise.all([loadCategories(), loadAccounts(), loadIncomeCategories()]);
  renderCategorySelect();
  refreshCheckBadge();
  flushQueue();
})();
