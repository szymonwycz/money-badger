/* Income calculator: any number of user-defined line items, each quantity × rate,
   plus adjustments, then an optional VAT and tax split. The items, their units and
   the two rates are all configuration — nothing here knows what the work is. */

const state = {
  month: INIT_MONTH,
  year:  INIT_YEAR,
  settings: null,
  items: [],
  adjustments: [],
};

// ── quantity input ────────────────────────────────────────────────────────────

/* Items flagged time_input take HH:MM and are stored as decimal hours, so 80:30
   is 80.5 — the rate is per hour either way. Everything else is a plain number. */
function parseQty(s, timeInput) {
  if (!s || !String(s).trim()) return 0;
  const str = String(s).trim();
  if (timeInput && str.includes(':')) {
    const [h, m] = str.split(':');
    return (parseInt(h) || 0) + (parseInt(m) || 0) / 60;
  }
  return parseFloat(str.replace(',', '.')) || 0;
}

function fmtQty(qty, timeInput) {
  if (!qty) return '';
  if (!timeInput) return String(Math.round(qty * 100) / 100);
  const h = Math.floor(qty);
  const m = Math.round((qty - h) * 60);
  return `${h}:${String(m).padStart(2, '0')}`;
}

// ── calculations ──────────────────────────────────────────────────────────────

/* Recomputed from the fields rather than from the server response, so the totals
   track what the user is typing before anything is saved. */
function readRows() {
  return state.items.map(it => {
    const qty  = parseQty(document.getElementById(`qty-${it.id}`).value, it.time_input);
    const rate = parseFloat(document.getElementById(`rate-${it.id}`).value) || 0;
    return { ...it, qty, rate, total: qty * rate };
  });
}

function calcResults() {
  const rows = readRows();
  const rateById = Object.fromEntries(rows.map(r => [r.id, r.rate]));

  for (const r of rows) {
    document.getElementById(`total-${r.id}`).textContent = r.total ? fmtNum(Math.round(r.total)) : '—';
  }

  const planNet = rows.reduce((s, r) => s + r.total, 0);
  const adjNet  = state.adjustments.reduce(
    (s, a) => s + a.sign * a.qty * (rateById[a.item_id] || 0), 0);

  const subtotal = planNet + adjNet;
  const vatRate  = state.settings.vat_rate;
  const taxRate  = state.settings.tax_rate;
  const vat      = vatRate != null ? subtotal * vatRate : null;
  const tax      = taxRate != null ? subtotal * taxRate : null;
  const gross    = subtotal + (vat || 0);

  const show = (n) => n ? fmtNum(Math.round(n)) : '—';
  document.getElementById('plan-net').textContent = show(planNet);
  document.getElementById('adj-net').textContent  = adjNet ? fmtNum(Math.round(adjNet)) : '—';
  document.getElementById('calc-gross').textContent       = show(gross);
  document.getElementById('calc-vat').textContent         = show(vat);
  document.getElementById('calc-tax').textContent         = show(tax);
  document.getElementById('calc-net-display').textContent = show(subtotal);

  return { subtotal, gross, vat, tax };
}

// ── rendering ─────────────────────────────────────────────────────────────────

function renderRows() {
  const el = document.getElementById('calc-rows');
  el.innerHTML = '';
  if (!state.items.length) {
    el.innerHTML = '<div class="calc-note">No items yet. Add one below — for example '
                 + '"Consulting", unit "h".</div>';
    return;
  }

  for (const it of state.items) {
    const row = document.createElement('div');
    row.className = 'calc-row';
    row.innerHTML = `
      <label class="calc-lbl">${esc(it.name)}${it.time_input ? ' (HH:MM)' : ''}</label>
      <input type="text" id="qty-${it.id}" class="calc-input-sm" placeholder="0"
             value="${esc(fmtQty(it.qty, it.time_input))}">
      <span class="calc-sep">×</span>
      <input type="number" id="rate-${it.id}" class="calc-input-rate" min="0" step="1"
             placeholder="rate" value="${it.rate || ''}">
      <span class="calc-sep">${esc(MB_SETTINGS.currency)}${it.unit ? '/' + esc(it.unit) : ''}</span>
      <span class="calc-result" id="total-${it.id}">—</span>
      <button class="icon-btn" title="Remove item" data-del="${it.id}">×</button>
    `;
    row.querySelector('[data-del]').addEventListener('click', () => deleteItem(it.id, it.name));
    for (const inp of row.querySelectorAll('input')) {
      inp.addEventListener('input', () => { calcResults(); renderAdjustments(); });
    }
    el.appendChild(row);
  }
}

function renderItemSelect() {
  const sel = document.getElementById('adj-item');
  sel.innerHTML = state.items
    .map(it => `<option value="${it.id}">${esc(it.name)}</option>`).join('');
}

function renderAdjustments() {
  const el = document.getElementById('adj-list');
  el.innerHTML = '';
  if (!state.adjustments.length) {
    el.innerHTML = '<div style="color:var(--text2);font-size:13px;padding:8px 0">No adjustments</div>';
    return;
  }

  const rows = readRows();
  const byId = Object.fromEntries(rows.map(r => [r.id, r]));

  for (const a of state.adjustments) {
    const item  = byId[a.item_id];
    const val   = a.sign * a.qty * (item ? item.rate : 0);
    const sign  = a.sign > 0 ? '+' : '−';
    const color = a.sign > 0 ? 'var(--green)' : 'var(--red)';
    const div   = document.createElement('div');
    div.className = 'adj-item';
    div.innerHTML = `
      <span class="adj-sign" style="color:${color}">${sign}</span>
      <span class="adj-hhmm">${esc(fmtQty(a.qty, item && item.time_input))}</span>
      <span class="adj-desc">${esc(a.description || (item ? item.name : ''))}</span>
      <span class="adj-val" style="color:${color}">${val ? fmtNum(Math.round(Math.abs(val))) : ''}</span>
      <button class="icon-btn" data-id="${a.id}">×</button>
    `;
    div.querySelector('.icon-btn').addEventListener('click', () => deleteAdj(a.id));
    el.appendChild(div);
  }
}

function renderSettings() {
  const s = state.settings;
  const pct = (r) => (r == null ? '' : Math.round(r * 10000) / 100);

  document.getElementById('vat-label').textContent = s.vat_label;
  document.getElementById('tax-label').textContent = s.tax_label;
  document.getElementById('rate-vat-label').value  = s.vat_label;
  document.getElementById('rate-tax-label').value  = s.tax_label;
  document.getElementById('rate-vat').value        = pct(s.vat_rate);
  document.getElementById('rate-tax').value        = pct(s.tax_rate);

  // A rate that doesn't apply shouldn't take up a slot in the summary bar.
  for (const [key, block, divider] of [['vat_rate', 'vat-block', 'vat-divider'],
                                       ['tax_rate', 'tax-block', 'tax-divider']]) {
    const visible = s[key] != null;
    document.getElementById(block).style.display   = visible ? '' : 'none';
    document.getElementById(divider).style.display = visible ? '' : 'none';
  }

  const targets = [
    s.income_category && `<strong>${esc(s.income_category)}</strong> in INCOME (planned)`,
    s.vat_rate != null && s.vat_category && `<strong>${esc(s.vat_category)}</strong>`,
    s.tax_rate != null && s.tax_category && `<strong>${esc(s.tax_category)}</strong>`,
  ].filter(Boolean);
  document.getElementById('apply-info').innerHTML =
    targets.length ? 'Applying will update ' + targets.join(', ') + '.'
                   : 'No target categories configured — set them up in the settings first.';
}

// ── load ──────────────────────────────────────────────────────────────────────

async function load() {
  const data = await api(`/api/calc?month=${state.month}&year=${state.year}`);
  state.settings    = data.settings;
  state.items       = data.items;
  state.adjustments = data.adjustments;

  renderSettings();
  renderRows();
  renderItemSelect();
  renderAdjustments();
  calcResults();
}

// ── saving ────────────────────────────────────────────────────────────────────

async function savePlan() {
  const entries = readRows().map(r => ({ item_id: r.id, qty: r.qty, rate: r.rate }));
  await api('/api/calc/plan', {
    method: 'PUT',
    body: JSON.stringify({ month: state.month, year: state.year, entries }),
  });
  const btn = document.getElementById('btn-save-plan');
  btn.textContent = 'Saved ✓';
  setTimeout(() => { btn.textContent = 'Save plan'; }, 1500);
}

async function saveRates() {
  const pct = (id) => {
    const v = document.getElementById(id).value.trim();
    return v === '' ? '' : String(parseFloat(v) / 100);
  };
  await api('/api/settings', {
    method: 'PUT',
    body: JSON.stringify({
      'calc.vat_rate':  pct('rate-vat'),
      'calc.tax_rate':  pct('rate-tax'),
      'calc.vat_label': document.getElementById('rate-vat-label').value.trim() || 'VAT',
      'calc.tax_label': document.getElementById('rate-tax-label').value.trim() || 'TAX',
    }),
  });
  await load();
}

async function addItem() {
  const name = document.getElementById('new-item-name').value.trim();
  if (!name) return;
  await api('/api/calc/items', {
    method: 'POST',
    body: JSON.stringify({
      name,
      unit: document.getElementById('new-item-unit').value.trim(),
      time_input: document.getElementById('new-item-time').checked,
      rate: 0,
    }),
  });
  document.getElementById('new-item-name').value = '';
  document.getElementById('new-item-unit').value = '';
  document.getElementById('new-item-time').checked = false;
  await load();
}

async function deleteItem(id, name) {
  if (!confirm(`Remove "${name}"? Its saved months and adjustments go with it.`)) return;
  await api(`/api/calc/items/${id}`, { method: 'DELETE' });
  await load();
}

async function addAdj() {
  const itemId = parseInt(document.getElementById('adj-item').value);
  if (!itemId) return;
  const item = state.items.find(i => i.id === itemId);
  const qty  = parseQty(document.getElementById('adj-qty').value, item && item.time_input);
  if (!qty) return;

  await api('/api/calc/adjustment', {
    method: 'POST',
    body: JSON.stringify({
      month: state.month, year: state.year, item_id: itemId,
      sign: document.getElementById('adj-sign').value, qty,
      description: document.getElementById('adj-desc').value.trim(),
    }),
  });
  document.getElementById('adj-qty').value = '';
  document.getElementById('adj-desc').value = '';
  await load();
}

async function deleteAdj(id) {
  await api(`/api/calc/adjustment/${id}`, { method: 'DELETE' });
  await load();
}

// ── apply ─────────────────────────────────────────────────────────────────────

async function apply() {
  const { subtotal } = calcResults();
  if (!subtotal) { toast('Fill in the plan first.', 'error'); return; }

  await savePlan();  // apply reads the stored month, not the fields
  const res = await api('/api/calc/apply', {
    method: 'POST',
    body: JSON.stringify({ month: state.month, year: state.year }),
  });
  if (res && res.error) return;  // api() already surfaced it

  const status = document.getElementById('apply-status');
  status.textContent = '✓ INCOME updated';
  status.style.color = 'var(--green)';
  setTimeout(() => { status.textContent = ''; }, 3000);
}

// ── navigation ────────────────────────────────────────────────────────────────

function updateLabel() {
  document.getElementById('month-label').textContent =
    `${MONTHS[state.month-1].toUpperCase()} ${state.year}`;

  /* Some contracts pay a month in arrears, so the work counted here was done in
     an earlier month. month_offset says how many. */
  const offset = (state.settings && state.settings.month_offset) || 0;
  const title = document.getElementById('plan-title');
  if (!offset) { title.textContent = 'PLAN'; return; }

  let m = state.month - offset, y = state.year;
  while (m < 1) { m += 12; y -= 1; }
  title.textContent = `PLAN — work from ${MONTHS[m-1]}${y !== state.year ? ' ' + y : ''}`
                    + ` → payout ${MONTHS[state.month-1]}`;
}

async function gotoMonth(m, y) {
  state.month = m; state.year = y;
  await load();
  updateLabel();
}

document.getElementById('btn-prev').addEventListener('click', () => {
  let m = state.month-1, y = state.year;
  if (m<1) { m=12; y--; }
  gotoMonth(m, y);
});
document.getElementById('btn-next').addEventListener('click', () => {
  let m = state.month+1, y = state.year;
  if (m>12) { m=1; y++; }
  gotoMonth(m, y);
});

document.getElementById('btn-save-plan').addEventListener('click', savePlan);
document.getElementById('btn-save-rates').addEventListener('click', saveRates);
document.getElementById('btn-add-item').addEventListener('click', addItem);
document.getElementById('new-item-name').addEventListener('keydown', e => { if (e.key==='Enter') addItem(); });
document.getElementById('btn-add-adj').addEventListener('click', addAdj);
document.getElementById('adj-qty').addEventListener('keydown', e => { if (e.key==='Enter') addAdj(); });
document.getElementById('btn-apply').addEventListener('click', apply);

load().then(updateLabel);
