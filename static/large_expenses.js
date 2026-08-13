const MONTH_NAMES = ['','JAN','FEB','MAR','APR','MAY','JUN','JUL','AUG','SEP','OCT','NOV','DEC'];
const expanded = new Set();


async function load() {
  const years = await api('/api/big-expenses/all');
  render(years);
}

function renderItem(e) {
  const row = document.createElement('div');
  row.className = `le-item${e.done ? ' done' : ''}`;
  row.dataset.id = e.id;

  let monthOpts = '<option value="">—</option>';
  for (let m = 1; m <= 12; m++) {
    monthOpts += `<option value="${m}"${e.planned_month == m ? ' selected' : ''}>${MONTH_NAMES[m]}</option>`;
  }

  row.innerHTML = `
    <input class="expense-cb" type="checkbox" ${e.done ? 'checked' : ''}>
    <input class="le-item-desc-input" type="text" value="${esc(e.description)}"
           style="flex:1;background:transparent;border:none;border-bottom:1px solid transparent;
                  color:${e.done ? 'var(--text2)' : 'var(--text)'};
                  font-size:var(--fs-base);padding:0 4px;
                  text-decoration:${e.done ? 'line-through' : 'none'}">
    <input class="le-item-amount-input" type="number" value="${e.amount}" min="0" step="1"
           style="width:80px;background:transparent;border:none;border-bottom:1px solid transparent;
                  color:${e.done ? 'var(--green)' : 'var(--yellow)'};
                  font-size:var(--fs-base);font-weight:600;text-align:right;padding:0 4px">
    <span style="color:var(--text2);font-size:var(--fs-base)">${MB_SETTINGS.currency}</span>
    <select class="le-month-sel">${monthOpts}</select>
    <button class="icon-btn le-del" title="Delete">×</button>
  `;

  // Checkbox toggle — done + sort
  row.querySelector('.expense-cb').addEventListener('change', async ev => {
    await api(`/api/big-expenses/${e.id}`, {
      method: 'PUT',
      body: JSON.stringify({
        description: row.querySelector('.le-item-desc-input').value,
        amount: parseFloat(row.querySelector('.le-item-amount-input').value) || e.amount,
        done: ev.target.checked,
        planned_month: parseInt(row.querySelector('.le-month-sel').value) || null,
      }),
    });
    load();
  });

  // Opis blur — zapisz
  row.querySelector('.le-item-desc-input').addEventListener('blur', async ev => {
    await api(`/api/big-expenses/${e.id}`, {
      method: 'PUT',
      body: JSON.stringify({
        description: ev.target.value,
        amount: parseFloat(row.querySelector('.le-item-amount-input').value) || e.amount,
        done: row.querySelector('.expense-cb').checked ? 1 : 0,
        planned_month: parseInt(row.querySelector('.le-month-sel').value) || null,
      }),
    });
  });

  // Kwota blur — zapisz
  row.querySelector('.le-item-amount-input').addEventListener('blur', async ev => {
    await api(`/api/big-expenses/${e.id}`, {
      method: 'PUT',
      body: JSON.stringify({
        description: row.querySelector('.le-item-desc-input').value,
        amount: parseFloat(ev.target.value) || e.amount,
        done: row.querySelector('.expense-cb').checked ? 1 : 0,
        planned_month: parseInt(row.querySelector('.le-month-sel').value) || null,
      }),
    });
  });

  // month changed — save
  row.querySelector('.le-month-sel').addEventListener('change', async ev => {
    await api(`/api/big-expenses/${e.id}`, {
      method: 'PUT',
      body: JSON.stringify({
        description: row.querySelector('.le-item-desc-input').value,
        amount: parseFloat(row.querySelector('.le-item-amount-input').value) || e.amount,
        done: row.querySelector('.expense-cb').checked ? 1 : 0,
        planned_month: parseInt(ev.target.value) || null,
      }),
    });
  });

  // Delete
  row.querySelector('.le-del').addEventListener('click', async ev => {
    ev.stopPropagation();
    if (!confirm(`Delete "${e.description}"?`)) return;
    await api(`/api/big-expenses/${e.id}`, { method: 'DELETE' });
    load();
  });

  // show the input borders while the row is hovered
  row.addEventListener('mouseenter', () => {
    row.querySelectorAll('.le-item-desc-input, .le-item-amount-input').forEach(i => {
      i.style.borderBottomColor = 'var(--border)';
    });
  });
  row.addEventListener('mouseleave', () => {
    row.querySelectorAll('.le-item-desc-input, .le-item-amount-input').forEach(i => {
      if (document.activeElement !== i) i.style.borderBottomColor = 'transparent';
    });
  });

  return row;
}

function render(years) {
  const el = document.getElementById('le-list');
  el.innerHTML = '';

  if (!years.length) {
    el.innerHTML = '<div style="color:var(--text2);padding:20px">No entries.</div>';
    return;
  }

  for (const yData of years) {
    const y   = yData.year;
    const pct = yData.total > 0 ? Math.round(yData.spent / yData.total * 100) : 0;
    const isExp = expanded.has(y);

    // Year header row
    const header = document.createElement('div');
    header.className = 'le-year-header';
    header.innerHTML = `
      <div class="le-year-left">
        <span class="le-toggle">${isExp ? '▼' : '▶'}</span>
        <span class="le-year-label">${y}</span>
      </div>
      <div class="le-year-right">
        <span class="le-spent">${fmtNum(yData.spent)}</span>
        <span class="le-sep">/</span>
        <span class="le-total">${fmtNum(yData.total)}</span>
        <div class="le-progress-bar">
          <div class="le-progress-fill" style="width:${pct}%"></div>
        </div>
      </div>
    `;
    clickable(header, () => {
      if (expanded.has(y)) expanded.delete(y);
      else expanded.add(y);
      load();
    });
    el.appendChild(header);

    if (!isExp) continue;

    // Items
    const itemsEl = document.createElement('div');
    itemsEl.className = 'le-items';

    const sortedItems = [...yData.items].sort((a, b) => (a.done || 0) - (b.done || 0));
    for (const e of sortedItems) {
      itemsEl.appendChild(renderItem(e));
    }
    el.appendChild(itemsEl);
  }
}

// populate year select with current + next 3 years
function populateYearSelect() {
  const sel = document.getElementById('expense-year');
  const now = new Date().getFullYear();
  sel.innerHTML = '';
  for (let y = now; y <= now + 3; y++) {
    sel.innerHTML += `<option value="${y}">${y}</option>`;
  }
}

// Add expense
document.getElementById('btn-add-expense').addEventListener('click', () => {
  populateYearSelect();
  document.getElementById('expense-desc').value   = '';
  document.getElementById('expense-amount').value = '';
  document.getElementById('expense-month').value  = '';
  document.getElementById('expense-modal').style.display = 'flex';
});
document.getElementById('expense-modal-close').addEventListener('click', () =>
  document.getElementById('expense-modal').style.display = 'none');
document.getElementById('expense-modal').addEventListener('click', e => {
  if (e.target === document.getElementById('expense-modal'))
    document.getElementById('expense-modal').style.display = 'none';
});
document.getElementById('btn-expense-save').addEventListener('click', async () => {
  const desc   = document.getElementById('expense-desc').value.trim();
  const amount = parseFloat(document.getElementById('expense-amount').value);
  const year   = parseInt(document.getElementById('expense-year').value);
  const month  = parseInt(document.getElementById('expense-month').value) || null;
  if (!desc || !amount || amount <= 0) return;
  await api('/api/big-expenses', { method: 'POST', body: JSON.stringify({ description: desc, amount, year, planned_month: month }) });
  document.getElementById('expense-modal').style.display = 'none';
  load();
});

// Auto-expand current year
expanded.add(new Date().getFullYear());
load();
