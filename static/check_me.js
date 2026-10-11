const state = {
  categories: [], incomeCategories: [], accounts: [], expenses: [], income: [],
  nonGoalIbans: [],
};
let goalTransaction = null;

async function loadRefs() {
  [state.categories, state.incomeCategories, state.accounts] = await Promise.all([
    api('/api/categories'),
    api('/api/income-categories'),
    api('/api/accounts'),
  ]);
}

async function loadUnreviewed() {
  const data = await api('/api/transactions/unreviewed');
  state.expenses = data.expenses || [];
  state.income   = data.income   || [];
  state.nonGoalIbans = data.non_goal_ibans || [];
}

function updateCount() {
  const n = state.expenses.length + state.income.length;
  document.getElementById('check-me-count').textContent = n ? `${n} to review` : 'all reviewed ✓';
}

function checkActions(tx) {
  const goal = tx.has_iban
    ? `<button class="btn-goal" type="button">TRANSFER TO GOAL <span>IBAN ••••${esc(tx.iban_last4)}</span></button>`
    : '';
  return `<div class="check-actions">${goal}<button class="btn-ignore" type="button">IGNORE</button></div>`;
}

function wireActions(tr, tx) {
  tr.querySelector('.btn-ignore').addEventListener('click', () => ignoreTransaction(tx.id));
  tr.querySelector('.btn-goal')?.addEventListener('click', () => openGoalModal(tx));
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
      <td class="tx-desc">${esc(fmtDesc(tx.description))}</td>
      <td><select class="parent-sel">${catOpts}</select></td>
      <td><select class="child-sel"><option value="">—</option></select></td>
      <td>${checkActions(tx)}</td>
    `;

    wireActions(tr, tx);

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
      <td class="tx-desc">${esc(fmtDesc(tx.description))}</td>
      <td><select class="income-cat-sel">${catOpts}</select></td>
      <td>${checkActions(tx)}</td>
    `;

    tr.querySelector('.income-cat-sel').addEventListener('change', e => {
      const cid = parseInt(e.target.value) || null;
      if (cid) assignIncomeCategory(tx.id, cid);
    });
    wireActions(tr, tx);

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

function renderGoalAccounts(tx) {
  const select = document.getElementById('goal-account-select');
  const mappedId = tx.counterpart_account_id;
  let options = state.accounts
    .filter(a => a.name !== tx.account || a.id === mappedId)
    .map(a => {
      const match = a.id === mappedId;
      return `<option value="${a.id}">${esc(a.name)}${match ? ' — IBAN match' : ''}</option>`;
    }).join('');
  if (mappedId && !state.accounts.some(a => a.id === mappedId)) {
    options += `<option value="${mappedId}">${esc(tx.counterpart_account_name)} — IBAN match</option>`;
  }
  select.innerHTML = '<option value="">— choose an account —</option>' + options
    + (mappedId ? '' : '<option value="__new__">＋ Create a new goal account…</option>');
}

function toggleNewGoalName() {
  const creating = document.getElementById('goal-account-select').value === '__new__';
  document.getElementById('goal-new-wrap').style.display = creating ? '' : 'none';
  if (creating) document.getElementById('goal-new-name').focus();
}

function openGoalModal(tx) {
  goalTransaction = tx;
  renderGoalAccounts(tx);
  document.getElementById('goal-account-select').value = tx.counterpart_account_id || '';
  document.getElementById('goal-new-name').value = '';
  document.getElementById('goal-new-wrap').style.display = 'none';
  document.getElementById('goal-iban-hint').textContent =
    `Counterparty IBAN ending in ${tx.iban_last4}`;
  document.getElementById('goal-modal').style.display = 'flex';
  document.getElementById('goal-account-select').focus();
}

function closeGoalModal() {
  goalTransaction = null;
  document.getElementById('goal-modal').style.display = 'none';
}

async function mapGoal() {
  if (!goalTransaction) return;
  const choice = document.getElementById('goal-account-select').value;
  if (!choice) { toast('Choose an account or create a new one', 'error'); return; }

  let body;
  if (choice === '__new__') {
    const name = document.getElementById('goal-new-name').value.trim();
    if (!name) { toast('Enter a name for the new goal account', 'error'); return; }
    if (!confirm(`Create asset account “${name}” with a starting balance of 0?`)) return;
    body = { new_account_name: name };
  } else {
    body = { account_id: Number(choice) };
  }

  try {
    await api(`/api/transactions/${goalTransaction.id}/goal-transfer`, {
      method: 'POST', body: JSON.stringify(body),
    });
    closeGoalModal();
    await loadRefs();
    await refresh();
    toast('Transfer mapped');
  } catch { /* api() already showed the error */ }
}

async function allowGoalDiscovery(decisionId) {
  if (!confirm('Allow this IBAN to be proposed as a goal again? Filed transactions stay unchanged.')) return;
  await api(`/api/non-goal-ibans/${decisionId}`, { method: 'DELETE' });
  await refresh();
  toast('Goal discovery restored');
}

function renderGoalDecisions() {
  const box = document.getElementById('goal-decisions');
  const list = document.getElementById('goal-decisions-list');
  box.hidden = !state.nonGoalIbans.length;
  list.innerHTML = '';
  for (const decision of state.nonGoalIbans) {
    const row = document.createElement('div');
    row.className = 'goal-decision-row';
    row.innerHTML = `<span>NOT A GOAL · IBAN ••••${esc(decision.iban_last4)}</span>
      <button type="button">ALLOW AGAIN</button>`;
    row.querySelector('button').addEventListener('click', () => allowGoalDiscovery(decision.id));
    list.appendChild(row);
  }
}

async function refresh() {
  await loadUnreviewed();
  updateCount();
  renderExpenses();
  renderIncome();
  renderGoalDecisions();
}

async function init() {
  await loadRefs();
  await refresh();
}

document.getElementById('goal-account-select').addEventListener('change', toggleNewGoalName);
document.getElementById('goal-map-submit').addEventListener('click', mapGoal);
document.getElementById('goal-modal-close').addEventListener('click', closeGoalModal);
document.getElementById('goal-modal').addEventListener('click', e => {
  if (e.target.id === 'goal-modal') closeGoalModal();
});
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && goalTransaction) closeGoalModal();
});

init();
