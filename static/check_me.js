const state = { categories: [], incomeCategories: [], expenses: [], income: [] };

async function loadRefs() {
  [state.categories, state.incomeCategories] = await Promise.all([
    api('/api/categories'),
    api('/api/income-categories'),
  ]);
}

async function loadUnreviewed() {
  const data = await api('/api/transactions/unreviewed');
  state.expenses = data.expenses || [];
  state.income   = data.income   || [];
}

function updateCount() {
  const n = state.expenses.length + state.income.length;
  document.getElementById('check-me-count').textContent = n ? `${n} to review` : 'all reviewed ✓';
}

function renderExpenses() {
  const tbody = document.getElementById('check-me-expenses-body');
  tbody.querySelectorAll('tr:not(#expenses-empty)').forEach(r => r.remove());
  const empty = document.getElementById('expenses-empty');
  empty.style.display = state.expenses.length ? 'none' : '';

  for (const tx of state.expenses) {
    const tr = document.createElement('tr');
    const catOpts = '<option value="">—</option>' + state.categories.map(g =>
      `<option value="${g.id}">${esc(g.name)}</option>`
    ).join('');

    tr.innerHTML = `
      <td class="tx-date">${formatDate(tx.date)}</td>
      <td class="tx-amount">${fmtNum(Math.abs(tx.amount))}</td>
      <td class="tx-acct">${esc(tx.account)}</td>
      <td class="tx-desc">${esc(tx.description)}</td>
      <td><select class="parent-sel">${catOpts}</select></td>
      <td><select class="child-sel"><option value="">—</option></select></td>
      <td><button class="btn-ignore" type="button">IGNORE</button></td>
    `;

    tr.querySelector('.btn-ignore').addEventListener('click', () => ignoreTransaction(tx.id));

    const parentSel = tr.querySelector('.parent-sel');
    const childSel  = tr.querySelector('.child-sel');

    parentSel.addEventListener('change', () => {
      const pid   = parseInt(parentSel.value) || null;
      const group = state.categories.find(g => g.id === pid);
      childSel.innerHTML = '<option value="">—</option>';
      if (group?.children.length) {
        for (const c of group.children) {
          childSel.innerHTML += `<option value="${c.id}">${esc(c.name)}</option>`;
        }
      } else if (pid) {
        assignExpenseCategory(tx.id, pid);
      }
    });

    childSel.addEventListener('change', () => {
      const cid = parseInt(childSel.value) || null;
      const pid = parseInt(parentSel.value) || null;
      if (cid || pid) assignExpenseCategory(tx.id, cid || pid);
    });

    tbody.appendChild(tr);
  }
}

function renderIncome() {
  const tbody = document.getElementById('check-me-income-body');
  tbody.querySelectorAll('tr:not(#income-empty)').forEach(r => r.remove());
  const empty = document.getElementById('income-empty');
  empty.style.display = state.income.length ? 'none' : '';

  for (const tx of state.income) {
    const tr = document.createElement('tr');
    const catOpts = '<option value="">—</option>' + state.incomeCategories.map(c =>
      `<option value="${c.id}">${esc(c.name)}</option>`
    ).join('');

    tr.innerHTML = `
      <td class="tx-date">${formatDate(tx.date)}</td>
      <td class="tx-amount">${fmtNum(tx.amount)}</td>
      <td class="tx-acct">${esc(tx.account)}</td>
      <td class="tx-desc">${esc(tx.description)}</td>
      <td><select class="income-cat-sel">${catOpts}</select></td>
      <td><button class="btn-ignore" type="button">IGNORE</button></td>
    `;

    tr.querySelector('.income-cat-sel').addEventListener('change', e => {
      const cid = parseInt(e.target.value) || null;
      if (cid) assignIncomeCategory(tx.id, cid);
    });
    tr.querySelector('.btn-ignore').addEventListener('click', () => ignoreTransaction(tx.id));

    tbody.appendChild(tr);
  }
}

async function assignExpenseCategory(txId, categoryId) {
  await api(`/api/transactions/${txId}`, {
    method: 'PUT',
    body: JSON.stringify({ category_id: categoryId }),
  });
  await refresh();
}

async function assignIncomeCategory(txId, incomeCategoryId) {
  await api(`/api/transactions/${txId}/income-category`, {
    method: 'PUT',
    body: JSON.stringify({ income_category_id: incomeCategoryId }),
  });
  await refresh();
}

async function ignoreTransaction(txId) {
  if (!confirm('Dismiss this transaction from CHECK ME? It stays uncategorized and there is no undo here.')) return;
  await api(`/api/transactions/${txId}/ignore`, { method: 'PUT' });
  await refresh();
}

async function refresh() {
  await loadUnreviewed();
  updateCount();
  renderExpenses();
  renderIncome();
}

async function init() {
  await loadRefs();
  await refresh();
}

init();
