const state = {
  month: INIT_MONTH,
  year:  INIT_YEAR,
  categories: [],
  categoriesLoaded: false,
  budget: { additionals: {}, spent: {}, income: 0, availableCash: 0 },
  expanded: new Set(),
  activeCat: null,
  editingAccountId: null,
  editingCategories: false,
};

// ── helpers ───────────────────────────────────────────────────────────────────

// ── data ─────────────────────────────────────────────────────────────────────

async function loadCategories() {
  state.categories = await api(`/api/categories?month=${state.month}&year=${state.year}`);
  if (!state.categoriesLoaded) {
    state.categories.forEach(g => state.expanded.add(g.id));
    state.categoriesLoaded = true;
  }
}

async function loadBudget() {
  const data = await api(`/api/budget?month=${state.month}&year=${state.year}`);
  state.budget.additionals    = data.additionals    || {};
  state.budget.spent          = data.spent          || {};
  state.budget.income         = data.income         || 0;
  state.budget.incomeReceived = data.income_received || 0;

  // real cash on hand (bank + cash accounts only — savings/goal "asset"
  // accounts don't count as money available to cover this month's overspend)
  state.budget.availableCash = availableCashFrom(await api('/api/accounts'));
}

// ── summary bar ───────────────────────────────────────────────────────────────

function updateSummary() { renderSummary(state); }

// ── tree ──────────────────────────────────────────────────────────────────────

function catTotal(cat) {
  const basic = cat.basic || 0;
  const adds  = (state.budget.additionals[String(cat.id)] || state.budget.additionals[cat.id] || [])
                  .reduce((s,a) => s + a.amount, 0);
  return basic + adds;
}

function groupTotal(group) {
  const leaves = group.children.length ? group.children : [group];
  return leaves.reduce((s, c) => s + catTotal(c), 0);
}

function groupSpent(group) {
  const leaves = group.children.length ? group.children : [group];
  let total = leaves.reduce((s, c) => s + Math.abs(state.budget.spent[c.id] || state.budget.spent[String(c.id)] || 0), 0);
  // spending booked directly on the parent group itself
  if (group.children.length) total += Math.abs(state.budget.spent[group.id] || state.budget.spent[String(group.id)] || 0);
  return total;
}

function getAdds(catId) {
  return state.budget.additionals[catId] || state.budget.additionals[String(catId)] || [];
}

function getSpent(catId) {
  return Math.abs(state.budget.spent[catId] || state.budget.spent[String(catId)] || 0);
}

function renderTree() {
  document.getElementById('month-label').textContent =
    `${MONTHS[state.month - 1].toUpperCase()} ${state.year}`;

  const tbody = document.getElementById('budget-tbody');
  tbody.innerHTML = '';

  for (const group of state.categories) {
    const isLeaf     = group.children.length === 0;
    const isExpanded = state.expanded.has(group.id);
    const gTotal     = groupTotal(group);
    const gSpent     = groupSpent(group);

    if (isLeaf) {
      tbody.appendChild(makeLeafGroupRow(group, getSpent(group.id)));
      const spacer = document.createElement('tr');
      spacer.className = 'group-spacer';
      spacer.innerHTML = '<td colspan="7"></td>';
      tbody.appendChild(spacer);
    } else {
      const tr = document.createElement('tr');
      tr.className = 'group-row';
      tr.dataset.groupId = group.id;
      const gOver      = gTotal > 0 && gSpent > gTotal;
      const gRemaining = Math.max(0, gTotal - gSpent);
      tr.innerHTML = `
        <td><div class="group-name-cell">
          <span class="toggle">${isExpanded ? '▼' : '▶'}</span>
          ${esc(group.name)}
          ${editControlsHtml(group.id, true)}
        </div></td>
        <td></td><td></td><td></td>
        <td class="group-total">${gTotal > 0 ? fmtNum(gTotal) : ''}</td>
        <td class="${gOver ? 'group-spent over' : 'group-spent'}">${gSpent > 0 ? fmtNum(gSpent) : ''}</td>
        <td class="remaining-val">${gTotal > 0 ? fmtNum(gRemaining) : ''}</td>
      `;
      clickable(tr, () => toggleGroup(group.id), `${group.name} — expand or collapse`);
      bindEditControls(tr, group.id, group.name);
      tbody.appendChild(tr);

      const sep = document.createElement('tr');
      sep.className = 'group-sep';
      sep.innerHTML = '<td colspan="7"></td>';
      tbody.appendChild(sep);

      if (isExpanded) {
        for (const child of group.children) {
          appendLeafRows(tbody, child);
        }
      }

      const spacer = document.createElement('tr');
      spacer.className = 'group-spacer';
      spacer.innerHTML = '<td colspan="7"></td>';
      tbody.appendChild(spacer);
    }
  }

  updateSummary();
  updateExpandBtn();
}

function updateExpandBtn() {
  const btn = document.getElementById('btn-expand-all');
  if (!btn) return;
  const hasCollapsed = state.categories.some(g => g.children.length > 0 && !state.expanded.has(g.id));
  btn.textContent = hasCollapsed ? 'EXPAND ALL' : 'COLLAPSE ALL';
}

function makeLeafGroupRow(cat, spent) {
  const adds      = getAdds(cat.id);
  const total     = catTotal(cat);
  const addAmount = adds.reduce((s,a) => s + a.amount, 0);
  const addNote   = adds.map(a => a.description).filter(Boolean).join(', ');
  const over      = total > 0 && spent > total;
  const remaining = Math.max(0, total - spent);

  const tr = document.createElement('tr');
  tr.className = 'group-row';
  tr.innerHTML = `
    <td><div class="group-name-cell">
      <span class="toggle"></span>
      ${esc(cat.name)}
      ${editControlsHtml(cat.id, true)}
    </div></td>
    <td class="basic-val">${cat.basic > 0 ? fmtNum(cat.basic) : '—'}</td>
    <td class="add-val">${addAmount > 0 ? fmtNum(addAmount) : ''}</td>
    <td class="note-val">${esc(addNote)}</td>
    <td class="group-total">${total > 0 ? fmtNum(total) : '—'}</td>
    <td class="${over ? 'group-spent over' : 'group-spent'}">${spent > 0 ? fmtNum(spent) : ''}</td>
    <td class="remaining-val">${total > 0 ? fmtNum(remaining) : ''}</td>
  `;
  clickable(tr, () => openModal(cat.id, cat.name, cat.basic), `${cat.name} — edit budget`);
  bindEditControls(tr, cat.id, cat.name);
  return tr;
}

function appendLeafRows(tbody, cat) {
  const adds  = getAdds(cat.id);
  const spent = getSpent(cat.id);
  const total = catTotal(cat);
  const over  = total > 0 && spent > total;
  const remaining = Math.max(0, total - spent);

  const addTotal = adds.reduce((s, a) => s + a.amount, 0);
  const addNote  = adds.map(a => a.description).filter(Boolean).join(', ');

  const tr = document.createElement('tr');
  tr.className = 'leaf-row';
  clickable(tr, () => openModal(cat.id, cat.name, cat.basic), `${cat.name} — edit budget`);
  tr.innerHTML = `
    <td class="leaf-name">${esc(cat.name)}${editControlsHtml(cat.id, false)}</td>
    <td class="basic-val">${cat.basic > 0 ? fmtNum(cat.basic) : '—'}</td>
    <td class="add-val">${addTotal > 0 ? fmtNum(addTotal) : ''}</td>
    <td class="note-val">${esc(addNote)}</td>
    <td class="total-val">${total > 0 ? fmtNum(total) : '—'}</td>
    <td class="${over ? 'spent-val over' : 'spent-val'}">${spent > 0 ? fmtNum(spent) : ''}</td>
    <td class="remaining-val">${total > 0 ? fmtNum(remaining) : ''}</td>
  `;
  bindEditControls(tr, cat.id, cat.name);
  tbody.appendChild(tr);
}

function toggleGroup(id) {
  if (state.expanded.has(id)) state.expanded.delete(id);
  else state.expanded.add(id);
  renderTree();
}

// ── category editing (ADD/REMOVE inline) ───────────────────────────────────────

function editControlsHtml(catId, allowAddSub) {
  if (!state.editingCategories) return '';
  const addBtn = allowAddSub
    ? `<button class="cat-edit-btn" data-action="add-sub" data-id="${catId}" title="Add subcategory">+</button>`
    : '';
  return `${addBtn}<button class="cat-edit-btn cat-remove-btn" data-action="remove" data-id="${catId}" title="Delete">×</button>`;
}

function bindEditControls(tr, catId, catName) {
  if (!state.editingCategories) return;
  const addBtn    = tr.querySelector('[data-action="add-sub"]');
  const removeBtn = tr.querySelector('[data-action="remove"]');
  if (addBtn) {
    addBtn.addEventListener('click', e => {
      e.stopPropagation();
      addCategoryPrompt(catId);
    });
  }
  if (removeBtn) {
    removeBtn.addEventListener('click', e => {
      e.stopPropagation();
      removeCategoryPrompt(catId, catName);
    });
  }
}

async function addCategoryPrompt(parentId) {
  const name = prompt(parentId ? 'New subcategory name:' : 'New category name:');
  if (!name || !name.trim()) return;
  const res = await api('/api/categories', {
    method: 'POST',
    body: JSON.stringify({ name: name.trim(), parent_id: parentId || null }),
  });
  if (res.error) { alert(res.error); return; }
  if (parentId) state.expanded.add(parentId);
  await loadCategories();
  renderTree();
}

async function removeCategoryPrompt(catId, catName) {
  if (!confirm(`Delete category "${catName}"?`)) return;
  const res = await api(`/api/categories/${catId}`, { method: 'DELETE' });
  if (res.error) { alert(res.error); return; }
  await loadCategories();
  renderTree();
}

// ── modal ─────────────────────────────────────────────────────────────────────

function openModal(catId, name, basic) {
  state.activeCat = { id: catId, name, basic: basic || 0 };
  document.getElementById('modal-title').textContent  = name;
  document.getElementById('basic-amount').value       = basic || '';
  document.getElementById('add-amount').value         = '';
  document.getElementById('add-desc').value           = '';
  renderAdditionalList();
  loadCatTransactions(catId);
  document.getElementById('modal').style.display = 'flex';
}

// ponytail: openModal is only ever called with a leaf category (see
// makeLeafGroupRow/appendLeafRows) — no parent-vs-children rollup needed here.
async function loadCatTransactions(catId) {
  const el = document.getElementById('tx-list');
  el.innerHTML = '<div class="expenses-empty" style="margin-bottom:8px">Loading…</div>';
  const txs = await api(`/api/transactions?month=${state.month}&year=${state.year}&category_id=${catId}`);
  if (!state.activeCat || state.activeCat.id !== catId) return; // modal moved on while fetching
  el.innerHTML = '';
  if (!txs.length) {
    el.innerHTML = '<div class="expenses-empty" style="margin-bottom:8px">No transactions this month</div>';
    return;
  }
  for (const t of txs) {
    const item = document.createElement('div');
    item.className = 'additional-item';
    item.innerHTML = `
      <span class="add-item-amount">${fmtNum(Math.abs(t.amount))}</span>
      <span class="add-item-desc">${formatTxDate(t.date)} — ${esc(t.description || '—')}</span>
    `;
    el.appendChild(item);
  }
}

function formatTxDate(dateStr) {
  if (!dateStr) return '';
  const [y, m, d] = dateStr.substring(0, 10).split('-');
  return `${d}/${m}`;
}

function renderAdditionalList() {
  const el   = document.getElementById('additional-list');
  const adds = getAdds(state.activeCat.id);
  el.innerHTML = '';

  if (!adds.length) {
    el.innerHTML = '<div class="expenses-empty" style="margin-bottom:8px">No entries this month</div>';
    return;
  }
  for (const a of adds) {
    const item = document.createElement('div');
    item.className = 'additional-item';
    item.innerHTML = `
      <span class="add-item-amount">${fmtNum(a.amount)}</span>
      <span class="add-item-desc">${esc(a.description || '—')}</span>
      <button class="icon-btn" data-id="${a.id}">×</button>
    `;
    item.querySelector('.icon-btn').addEventListener('click', () => deleteAdditional(a.id));
    el.appendChild(item);
  }
}

async function saveBasic() {
  const amount = parseFloat(document.getElementById('basic-amount').value) || 0;
  await api('/api/basic', {
    method: 'POST',
    body: JSON.stringify({ category_id: state.activeCat.id, amount, month: state.month, year: state.year }),
  });
  const cat = findCat(state.activeCat.id);
  if (cat) cat.basic = amount;
  state.activeCat.basic = amount;
  renderTree();
}

async function addAdditional() {
  const amount = parseFloat(document.getElementById('add-amount').value);
  const desc   = document.getElementById('add-desc').value.trim();
  if (!amount || amount <= 0) return;
  await api('/api/additional', {
    method: 'POST',
    body: JSON.stringify({ category_id: state.activeCat.id, amount, description: desc,
                           month: state.month, year: state.year }),
  });
  document.getElementById('add-amount').value = '';
  document.getElementById('add-desc').value   = '';
  await loadBudget();
  renderAdditionalList();
  renderTree();
}

async function deleteAdditional(id) {
  const entry = (state.budget.additionals[String(state.activeCat)]
              || state.budget.additionals[state.activeCat] || []).find(a => a.id === id);
  const what  = entry ? `${fmtNum(entry.amount)}${entry.description ? ' — ' + entry.description : ''}`
                      : 'this entry';
  if (!confirm(`Delete additional budget entry ${what}?`)) return;
  await api(`/api/additional/${id}`, { method: 'DELETE' });
  toast('Deleted');
  await loadBudget();
  renderAdditionalList();
  renderTree();
}

function findCat(id) {
  for (const g of state.categories) {
    if (g.id === id) return g;
    for (const c of g.children) { if (c.id === id) return c; }
  }
  return null;
}

// ── BALANCE bar ───────────────────────────────────────────────────────────────

async function loadAccounts() {
  const list = await api('/api/accounts');
  renderBalanceBar(list);
}

function renderBalanceBar(list) {
  const elAccounts = document.getElementById('balance-items-accounts');
  const elAssets   = document.getElementById('balance-items-assets');
  elAccounts.innerHTML = '';
  elAssets.innerHTML   = '';

  for (const acct of list) {
    const item = document.createElement('div');
    item.className = 'balance-item';
    item.title     = `Click to edit balance: ${acct.name}`;

    if (acct.type === 'asset') {
      const color = acct.balance < 0 ? 'var(--red)' : 'var(--teal)';
      item.innerHTML = `
        <span class="balance-name">${esc(acct.name)}</span>
        <span class="balance-val" style="color:${color}">${fmtNum(acct.balance)}</span>
      `;
      clickable(item, () => openAccountModal(acct.id, acct.name, acct.balance), `${acct.name} — correct balance`);
      elAssets.appendChild(item);
    } else {
      // bank or cash
      const color = acct.balance < 0 ? 'var(--red)' : 'var(--green)';
      item.innerHTML = `
        <span class="balance-name">${esc(acct.name)}</span>
        <span class="balance-val" style="color:${color}">${fmtNum(acct.balance)}</span>
      `;
      clickable(item, () => openAccountModal(acct.id, acct.name, acct.balance), `${acct.name} — correct balance`);
      elAccounts.appendChild(item);
    }
  }
}

function openAccountModal(id, name, balance) {
  state.editingAccountId = id;
  document.getElementById('account-modal-title').textContent = `Balance: ${name}`;
  document.getElementById('account-balance-input').value     = balance || '';
  document.getElementById('account-modal').style.display     = 'flex';
}

async function saveAccountBalance() {
  const balance = parseFloat(document.getElementById('account-balance-input').value) || 0;
  await api(`/api/accounts/${state.editingAccountId}`, {
    method: 'PUT',
    body: JSON.stringify({ balance }),
  });
  document.getElementById('account-modal').style.display = 'none';
  await loadAccounts();
}

async function deleteAccount() {
  const name = document.getElementById('account-modal-title').textContent;
  if (!confirm(`Delete account? (${name})`)) return;
  await api(`/api/accounts/${state.editingAccountId}`, { method: 'DELETE' });
  document.getElementById('account-modal').style.display = 'none';
  await loadAccounts();
}

// ── navigation ────────────────────────────────────────────────────────────────

async function gotoMonth(m, y) {
  state.month = m; state.year = y;
  await Promise.all([loadCategories(), loadBudget()]);
  renderTree();
}

// ── event listeners ───────────────────────────────────────────────────────────

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

document.getElementById('modal-close').addEventListener('click', () =>
  document.getElementById('modal').style.display = 'none');
document.getElementById('modal').addEventListener('click', e => {
  if (e.target === document.getElementById('modal'))
    document.getElementById('modal').style.display = 'none';
});

document.getElementById('btn-save-basic').addEventListener('click', saveBasic);
document.getElementById('basic-amount').addEventListener('keydown', e => { if (e.key==='Enter') saveBasic(); });
document.getElementById('btn-add-additional').addEventListener('click', addAdditional);
document.getElementById('add-desc').addEventListener('keydown', e => { if (e.key==='Enter') addAdditional(); });

// account modal
document.getElementById('account-modal-close').addEventListener('click', () =>
  document.getElementById('account-modal').style.display = 'none');
document.getElementById('account-modal').addEventListener('click', e => {
  if (e.target === document.getElementById('account-modal'))
    document.getElementById('account-modal').style.display = 'none';
});
document.getElementById('btn-account-save').addEventListener('click', saveAccountBalance);
document.getElementById('btn-account-delete').addEventListener('click', deleteAccount);
document.getElementById('account-balance-input').addEventListener('keydown', e => {
  if (e.key === 'Enter') saveAccountBalance();
});

// add account
document.getElementById('btn-add-account').addEventListener('click', () => {
  document.getElementById('new-account-name').value = '';
  document.getElementById('new-account-type').value = 'bank';
  document.getElementById('add-account-modal').style.display = 'flex';
});
document.getElementById('btn-add-account-asset').addEventListener('click', () => {
  document.getElementById('new-account-name').value = '';
  document.getElementById('new-account-type').value = 'asset';
  document.getElementById('add-account-modal').style.display = 'flex';
});
document.getElementById('add-account-modal-close').addEventListener('click', () =>
  document.getElementById('add-account-modal').style.display = 'none');
document.getElementById('add-account-modal').addEventListener('click', e => {
  if (e.target === document.getElementById('add-account-modal'))
    document.getElementById('add-account-modal').style.display = 'none';
});
document.getElementById('btn-add-account-save').addEventListener('click', async () => {
  const name = document.getElementById('new-account-name').value.trim();
  const type = document.getElementById('new-account-type').value;
  if (!name) return;
  await api('/api/accounts', { method: 'POST', body: JSON.stringify({ name, type }) });
  document.getElementById('add-account-modal').style.display = 'none';
  await loadAccounts();
});

document.getElementById('btn-copy-basic').addEventListener('click', async () => {
  const btn = document.getElementById('btn-copy-basic');
  const original = btn.textContent;
  await api('/api/basic/copy-next', {
    method: 'POST',
    body: JSON.stringify({ month: state.month, year: state.year }),
  });
  btn.textContent = 'Copied ✓';
  setTimeout(() => { btn.textContent = original; }, 1500);
});

document.getElementById('btn-expand-all').addEventListener('click', () => {
  const hasCollapsed = state.categories.some(g => g.children.length > 0 && !state.expanded.has(g.id));
  if (hasCollapsed) {
    state.categories.forEach(g => { if (g.children.length > 0) state.expanded.add(g.id); });
  } else {
    state.categories.forEach(g => state.expanded.delete(g.id));
  }
  renderTree();
});

document.getElementById('btn-edit-categories').addEventListener('click', () => {
  state.editingCategories = !state.editingCategories;
  const btn = document.getElementById('btn-edit-categories');
  btn.textContent = state.editingCategories ? 'DONE' : 'EDIT CATEGORIES';
  document.getElementById('btn-add-top-category').style.display = state.editingCategories ? '' : 'none';
  renderTree();
});

document.getElementById('btn-add-top-category').addEventListener('click', () => addCategoryPrompt(null));

// ── init ──────────────────────────────────────────────────────────────────────

async function init() {
  await loadCategories();
  await loadBudget();
  renderTree();
  loadAccounts();
}

init();
