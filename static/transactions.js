const state = {
  month: INIT_MONTH,
  year:  INIT_YEAR,
  categories: [],
  incomeCategories: [],
  accounts: [],
  allTxs: [],
  budget: { additionals: {}, spent: {}, income: 0 },
};

const TX_TYPES = ['Expense', 'Income', 'Money Transfer'];

// Destination-account options, shared by the transfer row and the picker below.
const toAcctOptions = tx => state.accounts.filter(a => a.name !== tx.account).map(a =>
  `<option value="${esc(a.name)}"${a.name === tx.account_to ? ' selected' : ''}>${esc(a.name)}</option>`
).join('');

// The server refuses a transfer with no destination, but the destination picker
// only renders on rows that already ARE transfers — so a row could never be
// converted. Ask here, in the description cell, and send both fields at once.
// Resolves '' when the user clicks away without choosing.
function askDestination(tr, tx) {
  const cell = tr.querySelector('.tx-desc');
  cell.innerHTML = `<select style="width:100%;background:transparent;border:1px solid var(--blue);color:inherit;font-size:inherit;padding:0">
      <option value="">Choose account…</option>${toAcctOptions(tx)}
    </select>`;
  const sel = cell.querySelector('select');
  sel.focus();
  return new Promise(resolve => {
    let done = false;
    const finish = v => { if (!done) { done = true; resolve(v); } };
    sel.addEventListener('change', () => finish(sel.value));
    sel.addEventListener('blur',   () => setTimeout(() => finish(''), 150));
  });
}

async function loadCategories() {
  // month/year matter: BASIC amounts are per-month and feed the summary bar
  state.categories = await api(`/api/categories?month=${state.month}&year=${state.year}`);
}

async function loadIncomeCategories() {
  state.incomeCategories = await api('/api/income-categories');
}

async function loadAccounts() {
  state.accounts = await api('/api/accounts');
  renderBalanceBars(state.accounts);
}

async function loadBudget() {
  const data = await api(`/api/budget?month=${state.month}&year=${state.year}`);
  state.budget.additionals = data.additionals || {};
  state.budget.spent       = data.spent       || {};
  state.budget.income      = data.income      || 0;
  state.budget.incomeReceived = data.income_received || 0;
  state.budget.availableCash  = availableCashFrom(state.accounts);
}

async function loadTransactions() {
  state.allTxs = await api(`/api/transactions?month=${state.month}&year=${state.year}`);
  applyFilters();
}

function applyFilters() {
  const descQ   = document.getElementById('search-desc').value.trim().toLowerCase();
  const amountQ = parseFloat(document.getElementById('search-amount').value);

  let txs = state.allTxs;
  if (descQ) txs = txs.filter(t => (t.description||'').toLowerCase().includes(descQ));
  if (!isNaN(amountQ) && amountQ > 0) {
    txs = txs.filter(t => Math.abs(t.amount - amountQ) < 0.01
                       || String(Math.abs(t.amount)).startsWith(String(amountQ)));
  }

  document.getElementById('tx-count').textContent = `${txs.length} transactions`;
  renderTable(txs);
}

function updateSummary() { renderSummary(state); }

function populateAddRowDropdowns() {
  const acctSel = document.getElementById('new-account');
  acctSel.innerHTML = '<option value="">Account</option>';
  for (const a of state.accounts) {
    acctSel.innerHTML += `<option value="${esc(a.name)}">${esc(a.name)}</option>`;
  }

  const toSel = document.getElementById('new-account-to');
  toSel.innerHTML = '<option value="">To account</option>';
  for (const a of state.accounts) {
    toSel.innerHTML += `<option value="${esc(a.name)}">${esc(a.name)}</option>`;
  }

  const incSel = document.getElementById('new-income-cat');
  incSel.innerHTML = '<option value="">Income category</option>';
  for (const c of state.incomeCategories) {
    incSel.innerHTML += `<option value="${c.id}">${esc(c.name)}</option>`;
  }

  // Each type owns different fields — show only the ones it uses, so the add row
  // can't build a transfer with a category or an income with a destination account.
  const typeSel = document.getElementById('new-type');
  const showFields = () => {
    const t = typeSel.value;
    toSel.hidden  = t !== 'Money Transfer';
    incSel.hidden = t !== 'Income';
    document.getElementById('new-cat').hidden     = t !== 'Expense';
    document.getElementById('new-subcat').hidden  = t !== 'Expense';
  };
  typeSel.addEventListener('change', showFields);
  showFields();

  const catSel = document.getElementById('new-cat');
  catSel.innerHTML = '<option value="">Category</option>';
  for (const g of state.categories) {
    catSel.innerHTML += `<option value="${g.id}">${esc(g.name)}</option>`;
  }

  catSel.addEventListener('change', () => {
    const pid    = parseInt(catSel.value);
    const subSel = document.getElementById('new-subcat');
    const group  = state.categories.find(g => g.id === pid);
    subSel.innerHTML = '<option value="">Subcategory</option>';
    if (group && group.children.length) {
      for (const c of group.children) {
        subSel.innerHTML += `<option value="${c.id}">${esc(c.name)}</option>`;
      }
    }
  });
}

function renderTable(txs) {
  const tbody = document.getElementById('tx-body');
  const rows  = tbody.querySelectorAll('tr:not(#add-row):not(#tx-empty)');
  rows.forEach(r => r.remove());

  const empty = document.getElementById('tx-empty');
  if (!txs.length) { empty.style.display = ''; return; }
  empty.style.display = 'none';

  for (const tx of txs) {
    let parentId = null, childId = null;
    if (tx.category_id) {
      const group = state.categories.find(g =>
        g.id === tx.category_id || g.children.some(c => c.id === tx.category_id)
      );
      if (group) {
        parentId = group.id;
        childId  = group.id === tx.category_id ? null : tx.category_id;
      }
    }

    const tr = document.createElement('tr');
    if (tx.tx_type === 'Money Transfer') tr.classList.add('tx-transfer');

    const acctOpts = state.accounts.map(a =>
      `<option value="${esc(a.name)}"${a.name === tx.account ? ' selected' : ''}>${esc(a.name)}</option>`
    ).join('');

    // TYPE — Balance Adjust is a system-generated correction, not user-editable
    const typeCell = tx.tx_type === 'Balance Adjust'
      ? `<span style="color:var(--text2)">Balance Adjust</span>`
      : `<select class="type-sel" style="background:transparent;border:none;color:var(--text2);font-size:inherit;padding:0">
          ${TX_TYPES.map(t => `<option value="${t}"${t === tx.tx_type ? ' selected' : ''}>${t === 'Money Transfer' ? 'Transfer' : t}</option>`).join('')}
        </select>`;

    // DESCRIPTION — a Transfer needs a destination account, not free text
    const descCell = tx.tx_type === 'Money Transfer'
      ? `<select class="to-acct-sel" style="width:100%;background:transparent;border:none;color:inherit;font-size:inherit;padding:0">
          <option value="">Choose account</option>
          ${toAcctOptions(tx)}
        </select>`
      : `<input type="text" value="${esc(tx.description)}" style="width:100%;background:transparent;border:none;color:inherit;font-size:inherit;padding:0" data-field="description">`;

    // CATEGORY / SUBCATEGORY — Transfer has neither, Income uses the flat income-category list
    let catCell, subCell;
    if (tx.tx_type === 'Money Transfer') {
      catCell = `<span style="color:var(--text2)">—</span>`;
      subCell = '';
    } else if (tx.tx_type === 'Income') {
      const incOpts = '<option value="">—</option>' + state.incomeCategories.map(c =>
        `<option value="${c.id}"${c.id === tx.income_category_id ? ' selected' : ''}>${esc(c.name)}</option>`).join('');
      catCell = `<select class="income-cat-sel" style="background:transparent;border:none;color:var(--text2);font-size:inherit;padding:0;max-width:125px">${incOpts}</select>`;
      subCell = '';
    } else {
      const catOpts = '<option value="">—</option>' + state.categories.map(g =>
        `<option value="${g.id}"${g.id === parentId ? ' selected' : ''}>${esc(g.name)}</option>`
      ).join('');
      const parentGroup = state.categories.find(g => g.id === parentId);
      const subOpts = '<option value="">—</option>' + (parentGroup?.children || []).map(c =>
        `<option value="${c.id}"${c.id === childId ? ' selected' : ''}>${esc(c.name)}</option>`
      ).join('');
      catCell = `<select class="parent-sel" style="background:transparent;border:none;color:var(--text2);font-size:inherit;padding:0;max-width:125px">${catOpts}</select>`;
      subCell = `<select class="child-sel" style="background:transparent;border:none;color:var(--text2);font-size:inherit;padding:0;max-width:125px">${subOpts}</select>`;
    }

    tr.innerHTML = `
      <td class="tx-date">
        <input type="text" value="${formatDate(tx.date)}" style="width:82px;background:transparent;border:none;color:inherit;font-size:inherit;padding:0" data-field="date">
      </td>
      <td class="tx-amount" style="white-space:nowrap">
        <input type="number" value="${fmtAmountInput(Math.abs(tx.amount))}" ${tx.tx_type === 'Balance Adjust' ? 'readonly' : ''} style="width:85px;background:transparent;border:none;color:${amountColor(tx)};font-size:inherit;font-weight:600;padding:0;text-align:right" data-field="amount"> ${MB_SETTINGS.currency}
      </td>
      <td class="tx-acct">
        <select style="background:transparent;border:none;color:var(--text2);font-size:inherit;padding:0;max-width:105px" data-field="account">
          <option value="">—</option>${acctOpts}
        </select>
      </td>
      <td style="white-space:nowrap">${typeCell}</td>
      <td class="tx-desc" style="word-break:break-word;max-width:200px">${descCell}</td>
      <td style="white-space:nowrap">${catCell}</td>
      <td style="white-space:nowrap;max-width:125px;overflow:hidden;text-overflow:ellipsis">${subCell}</td>
      <td style="text-align:right"><button class="icon-btn tx-del" title="Delete transaction">×</button></td>
    `;

    const saveField = async (field, value) => {
      await api(`/api/transactions/${tx.id}`, {
        method: 'PUT',
        body: JSON.stringify({ [field]: value }),
      });
      toast('Saved');
      await loadAccounts();
    };

    tr.querySelectorAll('[data-field]').forEach(input => {
      input.addEventListener('blur',    () => saveField(input.dataset.field, input.value));
      input.addEventListener('keydown', e  => { if (e.key === 'Enter') input.blur(); });
      input.addEventListener('click',   e  => e.stopPropagation());
    });

    const typeSel = tr.querySelector('.type-sel');
    if (typeSel) {
      typeSel.addEventListener('click', e => e.stopPropagation());
      typeSel.addEventListener('change', async () => {
        const body = { tx_type: typeSel.value };
        if (typeSel.value === 'Money Transfer' && !tx.account_to) {
          const dest = await askDestination(tr, tx);
          if (!dest) { await loadTransactions(); return; }
          body.account_to = dest;
        }
        await api(`/api/transactions/${tx.id}`, { method: 'PUT', body: JSON.stringify(body) });
        await Promise.all([loadAccounts(), loadTransactions()]);
      });
    }

    const toAcctSel = tr.querySelector('.to-acct-sel');
    if (toAcctSel) {
      toAcctSel.addEventListener('click', e => e.stopPropagation());
      toAcctSel.addEventListener('change', () => saveField('account_to', toAcctSel.value));
    }

    const incomeCatSel = tr.querySelector('.income-cat-sel');
    if (incomeCatSel) {
      incomeCatSel.addEventListener('click', e => e.stopPropagation());
      incomeCatSel.addEventListener('change', async () => {
        const cid = parseInt(incomeCatSel.value) || null;
        if (!cid) return;
        await api(`/api/transactions/${tx.id}/income-category`, {
          method: 'PUT',
          body: JSON.stringify({ income_category_id: cid }),
        });
      });
    }

    const parentSel = tr.querySelector('.parent-sel');
    const childSel  = tr.querySelector('.child-sel');

    if (parentSel) parentSel.addEventListener('change', () => {
      const newPid = parseInt(parentSel.value) || null;
      const group  = state.categories.find(g => g.id === newPid);
      childSel.innerHTML = '<option value="">—</option>';
      if (group?.children.length) {
        for (const c of group.children) {
          childSel.innerHTML += `<option value="${c.id}">${esc(c.name)}</option>`;
        }
      } else {
        saveTxCategory(tx.id, newPid);
      }
    });

    if (childSel) childSel.addEventListener('change', () => {
      const cid = parseInt(childSel.value) || null;
      const pid = parseInt(parentSel.value) || null;
      saveTxCategory(tx.id, cid || pid);
    });

    tr.querySelector('.tx-del').addEventListener('click', async () => {
      if (!confirm(`Delete transaction "${tx.description || formatDate(tx.date)}" (${fmtNum(Math.abs(tx.amount))})?`)) return;
      await api(`/api/transactions/${tx.id}`, { method: 'DELETE' });
      await Promise.all([loadAccounts(), loadTransactions()]);
    });

    tbody.appendChild(tr);
  }
}

async function saveTxCategory(txId, categoryId) {
  await api(`/api/transactions/${txId}`, {
    method: 'PUT',
    body: JSON.stringify({ category_id: categoryId }),
  });
}

function updateMonthLabel() {
  document.getElementById('month-label').textContent =
    `${MONTHS[state.month - 1].toUpperCase()} ${state.year}`;
}

async function gotoMonth(m, y) {
  state.month = m; state.year = y;
  updateMonthLabel();
  await Promise.all([loadCategories(), loadBudget(), loadTransactions()]);
  updateSummary();
}

document.getElementById('btn-prev').addEventListener('click', () => {
  let m = state.month - 1, y = state.year;
  if (m < 1) { m = 12; y--; }
  gotoMonth(m, y);
});
document.getElementById('btn-next').addEventListener('click', () => {
  let m = state.month + 1, y = state.year;
  if (m > 12) { m = 1; y++; }
  gotoMonth(m, y);
});

document.getElementById('search-desc').addEventListener('input', applyFilters);
document.getElementById('search-amount').addEventListener('input', applyFilters);

document.getElementById('btn-add-tx').addEventListener('click', async () => {
  const date   = document.getElementById('new-date').value.trim();
  const amount = parseFloat(document.getElementById('new-amount').value);
  const acct   = document.getElementById('new-account').value;
  const desc   = document.getElementById('new-desc').value.trim();
  const catSel = document.getElementById('new-cat');
  const subSel = document.getElementById('new-subcat');
  const catId  = parseInt(subSel.value) || parseInt(catSel.value) || null;

  // used to `return` silently: clicking + with an empty field did nothing at all,
  // no message, no highlight
  if (!date)   { toast('Pick a date', 'error');   return; }
  if (!amount) { toast('Enter an amount', 'error'); return; }

  const type = document.getElementById('new-type').value;
  const body = { date, amount, account: acct, description: desc, tx_type: type };
  if (type === 'Money Transfer') {
    body.account_to = document.getElementById('new-account-to').value;
    if (!acct || !body.account_to) { toast('A transfer needs both accounts', 'error'); return; }
    if (acct === body.account_to)  { toast('Pick two different accounts', 'error'); return; }
  } else if (type === 'Income') {
    body.income_category_id = parseInt(document.getElementById('new-income-cat').value) || null;
    if (!acct) { toast('Income needs an account', 'error'); return; }
  } else {
    body.category_id = catId;
  }

  const btn = document.getElementById('btn-add-tx');
  btn.disabled = true;
  try {
    await api('/api/transactions', {
      method: 'POST',
      body: JSON.stringify(body),
    });
  } catch (e) {
    btn.disabled = false;
    return;   // the api() helper already showed the reason
  }
  btn.disabled = false;
  toast(`Added ${fmtNum(amount)}`);

  document.getElementById('new-date').value        = todayISO();
  document.getElementById('new-amount').value      = '';
  document.getElementById('new-desc').value        = '';
  document.getElementById('new-account').value     = '';
  document.getElementById('new-cat').value         = '';
  document.getElementById('new-subcat').innerHTML  = '<option value="">Subcategory</option>';
  document.getElementById('new-account-to').value  = '';
  document.getElementById('new-income-cat').value  = '';

  await Promise.all([loadAccounts(), loadTransactions()]);
});

function todayISO() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
}

async function init() {
  document.getElementById('new-date').value = todayISO();
  updateMonthLabel();
  await Promise.all([loadCategories(), loadIncomeCategories(), loadAccounts()]);
  populateAddRowDropdowns();
  await Promise.all([loadBudget(), loadTransactions()]);
  updateSummary();
}

init();
