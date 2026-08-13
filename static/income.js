const state = {
  month: INIT_MONTH,
  year:  INIT_YEAR,
  entries: [],     // [{category_id, name, planned, received, note}]
  accounts: [],
};

// ── load ─────────────────────────────────────────────────────────────────────

async function loadEntries() {
  state.entries = await api(`/api/income-entries?month=${state.month}&year=${state.year}`);
}

async function loadAccounts() {
  state.accounts = await api('/api/accounts');
  renderBalanceBars(state.accounts);
}

// ── summary bar ──────────────────────────────────────────────────────────────

function updateSummary() {
  const totalPlanned  = state.entries.reduce((s, e) => s + (e.planned  || 0), 0);
  const totalReceived = state.entries.reduce((s, e) => s + (e.received || 0), 0);
  const remaining     = totalPlanned - totalReceived;

  document.getElementById('sum-planned').textContent  = fmtNum(totalPlanned);
  document.getElementById('sum-received').textContent = fmtNum(totalReceived);

  const remEl = document.getElementById('sum-remaining');
  remEl.textContent = remaining > 0 ? `short ${fmtNum(remaining)}`
                    : remaining < 0 ? `over ${fmtNum(-remaining)}` : fmtNum(0);
  remEl.style.color = remaining > 0 ? 'var(--yellow)' : 'var(--green)';

  const narEl = document.getElementById('sum-narrative');
  if (totalPlanned === 0) {
    narEl.innerHTML = '<span style="color:var(--text2)">No plan for this month</span>';
  } else if (totalReceived >= totalPlanned) {
    const extra = totalReceived - totalPlanned;
    narEl.innerHTML = extra > 0
      ? `Surplus: <strong style="color:var(--green)">${fmtNum(extra)}</strong>`
      : `<strong style="color:var(--green)">Plan completed ✓</strong>`;
  } else {
    narEl.innerHTML = '';
  }
}

// ── table ────────────────────────────────────────────────────────────────────

function renderTable() {
  const tbody = document.getElementById('income-tbody');
  tbody.innerHTML = '';

  for (const e of state.entries) {
    const remaining = (e.planned || 0) - (e.received || 0);
    const remClass  = 'remaining-val';
    const remColor  = remaining < 0 ? 'var(--red)' : remaining === 0 ? 'var(--green)' : '';

    const tr = document.createElement('tr');
    tr.className = 'leaf-group-row';
    tr.innerHTML = `
      <td class="leaf-group-name" style="padding-left:12px">${esc(e.name)}</td>
      <td class="basic-val" style="white-space:nowrap">
        <input type="number" value="${e.planned  ? fmtAmountInput(e.planned)  : ''}" placeholder="0" min="0" step="1"
               style="width:90px;background:transparent;border:none;border-bottom:1px solid transparent;
                      color:var(--text2);font-size:var(--fs-base);text-align:right;padding:0 2px"
               data-field="planned" data-catid="${e.category_id}"> ${MB_SETTINGS.currency}
      </td>
      <td class="add-val" style="white-space:nowrap">
        <input type="number" value="${e.received ? fmtAmountInput(e.received) : ''}" placeholder="0" min="0" step="1"
               style="width:90px;background:transparent;border:none;border-bottom:1px solid transparent;
                      color:var(--green);font-size:var(--fs-base);text-align:right;padding:0 2px"
               data-field="received" data-catid="${e.category_id}"> ${MB_SETTINGS.currency}
      </td>
      <td class="note-val">
        <input type="text" value="${esc(e.note||'')}" placeholder="—"
               style="width:100%;background:transparent;border:none;border-bottom:1px solid transparent;
                      color:var(--text2);font-size:var(--fs-sm);padding:0 2px"
               data-field="note" data-catid="${e.category_id}">
      </td>
      <td class="${remClass}" style="${remColor ? 'color:'+remColor : ''}">
        ${remaining !== 0
            ? (remaining > 0 ? `short ${fmtNum(remaining)}` : `over ${fmtNum(-remaining)}`)
            : '<span style="color:var(--green)">✓</span>'}
      </td>
      <td style="text-align:right">
        <button class="icon-btn del-cat-btn" data-catid="${e.category_id}" title="Delete category">×</button>
      </td>
    `;

    // save on blur, for every input
    tr.querySelectorAll('input[data-field]').forEach(input => {
      const catId = parseInt(input.dataset.catid);
      input.addEventListener('focus', () => { input.style.borderBottomColor = 'var(--border)'; });
      input.addEventListener('blur',  () => {
        input.style.borderBottomColor = 'transparent';
        saveEntry(catId, input.dataset.field);
      });
      input.addEventListener('keydown', e => { if (e.key === 'Enter') input.blur(); });
    });

    // Hover — border na inputach
    tr.addEventListener('mouseenter', () => {
      tr.querySelectorAll('input').forEach(i => { i.style.borderBottomColor = 'var(--border)'; });
    });
    tr.addEventListener('mouseleave', () => {
      tr.querySelectorAll('input').forEach(i => {
        if (document.activeElement !== i) i.style.borderBottomColor = 'transparent';
      });
    });

    // Delete
    tr.querySelector('.del-cat-btn').addEventListener('click', async ev => {
      ev.stopPropagation();
      const catName = e.name;
      if (!confirm(`Delete category "${catName}"? This also deletes all its entries.`)) return;
      await api(`/api/income-categories/${e.category_id}`, { method: 'DELETE' });
      await loadEntries();
      renderTable();
      updateSummary();
    });

    tbody.appendChild(tr);
  }
}

// ── save entry ────────────────────────────────────────────────────────────────

async function saveEntry(catId, changedField) {
  // read the current values straight from the DOM
  const row = document.querySelector(`input[data-catid="${catId}"][data-field="planned"]`)?.closest('tr');
  if (!row) return;

  const planned  = parseFloat(row.querySelector('[data-field="planned"]').value)  || 0;
  const received = parseFloat(row.querySelector('[data-field="received"]').value) || 0;
  const note     = row.querySelector('[data-field="note"]').value;

  // Send only the field that was edited. `received` is maintained by the import
  // pipeline, so blindly PUTting the value this page happened to load — while
  // editing the note, say — wiped out income that landed in between.
  const payload = { category_id: catId, month: state.month, year: state.year };
  if (changedField === 'planned')  payload.planned  = planned;
  if (changedField === 'received') payload.received = received;
  if (changedField === 'note')     payload.note     = note;

  await api('/api/income-entries', {
    method: 'PUT',
    body: JSON.stringify(payload),
  });
  toast('Saved');

  // Zaktualizuj state lokalnie
  const entry = state.entries.find(e => e.category_id === catId);
  if (entry) { entry.planned = planned; entry.received = received; entry.note = note; }

  updateSummary();
  // Re-render only the REMAINING row; redrawing the whole tbody would steal focus
  const remCell = row.querySelector('.remaining-val');
  if (remCell) {
    const rem = planned - received;
    remCell.className = 'remaining-val';
    remCell.style.color = rem < 0 ? 'var(--red)' : rem === 0 ? 'var(--green)' : '';
    remCell.innerHTML = rem !== 0
      ? (rem > 0 ? `short ${fmtNum(rem)}` : `over ${fmtNum(-rem)}`)
      : '<span style="color:var(--green)">✓</span>';
  }
}

// ── navigation ────────────────────────────────────────────────────────────────

function updateMonthLabel() {
  document.getElementById('month-label').textContent =
    `${MONTHS[state.month - 1].toUpperCase()} ${state.year}`;
}

async function gotoMonth(m, y) {
  state.month = m; state.year = y;
  updateMonthLabel();
  await loadEntries();
  renderTable();
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

// ── add category ──────────────────────────────────────────────────────────────

document.getElementById('btn-add-cat').addEventListener('click', () => {
  document.getElementById('new-cat-name').value = '';
  document.getElementById('add-cat-modal').style.display = 'flex';
});
document.getElementById('add-cat-modal-close').addEventListener('click', () =>
  document.getElementById('add-cat-modal').style.display = 'none');
document.getElementById('add-cat-modal').addEventListener('click', e => {
  if (e.target === document.getElementById('add-cat-modal'))
    document.getElementById('add-cat-modal').style.display = 'none';
});
document.getElementById('btn-add-cat-save').addEventListener('click', async () => {
  const name = document.getElementById('new-cat-name').value.trim();
  if (!name) return;
  await api('/api/income-categories', { method: 'POST', body: JSON.stringify({ name }) });
  document.getElementById('add-cat-modal').style.display = 'none';
  await loadEntries();
  renderTable();
  updateSummary();
});
document.getElementById('new-cat-name').addEventListener('keydown', e => {
  if (e.key === 'Enter') document.getElementById('btn-add-cat-save').click();
});

// ── copy to next month ───────────────────────────────────────────────────────

document.getElementById('btn-copy-income').addEventListener('click', async () => {
  const btn = document.getElementById('btn-copy-income');
  const original = btn.textContent;
  await api('/api/income-entries/copy-next', {
    method: 'POST',
    body: JSON.stringify({ month: state.month, year: state.year }),
  });
  btn.textContent = 'Copied ✓';
  setTimeout(() => { btn.textContent = original; }, 1500);
});

// ── init ──────────────────────────────────────────────────────────────────────

async function init() {
  updateMonthLabel();
  await Promise.all([loadEntries(), loadAccounts()]);
  renderTable();
  updateSummary();
}

init();
