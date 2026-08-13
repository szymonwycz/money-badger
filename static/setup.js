/* First-run setup. Everything is assembled client-side and posted once, so
   nothing lands in the database until the user presses Finish. */

if (document.getElementById('presets')) {

const state = { preset: null, data: null };

// ── category tree ─────────────────────────────────────────────────────────────

function renderTree() {
  const el = document.getElementById('cat-tree');
  const groups = (state.data && state.data.categories) || [];
  if (!groups.length) {
    el.innerHTML = '<div class="hint" style="margin:0">No categories — you\'ll add '
                 + 'your own from the EXPENSES tab.</div>';
  } else {
    el.innerHTML = groups.map((g, gi) => `
      <div class="grp">
        <label><input type="checkbox" data-grp="${gi}" checked> ${esc(g.name)}</label>
        <div class="kids">${(g.children || []).map((c, ci) =>
          `<label><input type="checkbox" data-grp="${gi}" data-kid="${ci}" checked> ${esc(c)}</label>`
        ).join('')}</div>
      </div>`).join('');

    // Unchecking a group takes its children with it — keeping an orphaned
    // subcategory would create a category the user just said they didn't want.
    for (const box of el.querySelectorAll('input[data-grp]:not([data-kid])')) {
      box.addEventListener('change', () => {
        for (const kid of el.querySelectorAll(`input[data-grp="${box.dataset.grp}"][data-kid]`)) {
          kid.checked = box.checked;
          kid.disabled = !box.checked;
        }
      });
    }
  }

  const inc = document.getElementById('inc-tree');
  const incomes = (state.data && state.data.income_categories) || [];
  inc.innerHTML = incomes.length
    ? `<div class="kids" style="padding-left:0">${incomes.map((c, i) =>
        `<label><input type="checkbox" data-inc="${i}" checked ${c === 'CHECK ME' ? 'disabled' : ''}> ${esc(c)}</label>`
      ).join('')}</div>`
    : '<div class="hint" style="margin:0">None yet.</div>';
}

function collectCategories() {
  const el = document.getElementById('cat-tree');
  const groups = (state.data && state.data.categories) || [];
  return groups.map((g, gi) => {
    const on = el.querySelector(`input[data-grp="${gi}"]:not([data-kid])`);
    if (!on || !on.checked) return null;
    const children = (g.children || []).filter((_, ci) => {
      const k = el.querySelector(`input[data-grp="${gi}"][data-kid="${ci}"]`);
      return k && k.checked;
    });
    return { name: g.name, children };
  }).filter(Boolean);
}

function collectIncomes() {
  const el = document.getElementById('inc-tree');
  const incomes = (state.data && state.data.income_categories) || [];
  const picked = incomes.filter((_, i) => {
    const box = el.querySelector(`input[data-inc="${i}"]`);
    return box && box.checked;
  });
  // The review inbox is not optional — the importer writes that literal name.
  if (!picked.includes('CHECK ME')) picked.push('CHECK ME');
  return picked;
}

// ── accounts ──────────────────────────────────────────────────────────────────

function accountRow(name = '', type = 'bank') {
  const div = document.createElement('div');
  div.className = 'row';
  div.innerHTML = `
    <input type="text" value="${esc(name)}" placeholder="Account name">
    <select>
      <option value="bank"${type === 'bank' ? ' selected' : ''}>bank</option>
      <option value="cash"${type === 'cash' ? ' selected' : ''}>cash</option>
      <option value="asset"${type === 'asset' ? ' selected' : ''}>asset</option>
    </select>
    <button class="icon-btn" type="button">×</button>`;
  div.querySelector('.icon-btn').addEventListener('click', () => div.remove());
  return div;
}

function renderAccounts() {
  const el = document.getElementById('acct-rows');
  el.innerHTML = '';
  const accts = (state.data && state.data.accounts) || [];
  for (const a of accts) {
    const [name, type] = Array.isArray(a) ? a : [a.name, a.type || 'bank'];
    el.appendChild(accountRow(name, type));
  }
  if (!accts.length) el.appendChild(accountRow());
}

function collectAccounts() {
  return [...document.querySelectorAll('#acct-rows .row')].map(r => {
    const name = r.querySelector('input').value.trim();
    return name ? [name, r.querySelector('select').value] : null;
  }).filter(Boolean);
}

// ── calculator ────────────────────────────────────────────────────────────────

function calcRow() {
  const div = document.createElement('div');
  div.className = 'row';
  div.innerHTML = `
    <input type="text" placeholder="Item, e.g. Consulting">
    <input type="text" placeholder="unit" style="width:80px">
    <input type="number" placeholder="rate" style="width:110px" min="0" step="1">
    <label style="display:flex;align-items:center;gap:5px;font-size:13px;color:var(--text2)">
      <input type="checkbox"> HH:MM
    </label>
    <button class="icon-btn" type="button">×</button>`;
  div.querySelector('.icon-btn').addEventListener('click', () => div.remove());
  return div;
}

function collectCalcItems() {
  if (!document.getElementById('calc-on').checked) return [];
  return [...document.querySelectorAll('#calc-rows .row')].map(r => {
    const [name, unit, rate] = r.querySelectorAll('input[type=text], input[type=number]');
    return name.value.trim()
      ? { name: name.value.trim(), unit: unit.value.trim(),
          rate: parseFloat(rate.value) || 0,
          time_input: r.querySelector('input[type=checkbox]').checked }
      : null;
  }).filter(Boolean);
}

// ── currency ──────────────────────────────────────────────────────────────────

/* Symbol and number format are separate settings — the euro is written 1.234,56 €
   in Germany and €1,234.56 in Ireland — so each entry carries both. */
const CURRENCIES = [
  ['zł',  'pl-PL', 'Polish złoty'],
  ['€',   'de-DE', 'Euro — 1.234,56'],
  ['€',   'en-IE', 'Euro — 1,234.56'],
  ['$',   'en-US', 'US dollar'],
  ['£',   'en-GB', 'Pound sterling'],
  ['CHF', 'de-CH', 'Swiss franc'],
  ['Kč',  'cs-CZ', 'Czech koruna'],
  ['kr',  'sv-SE', 'Swedish krona'],
  ['kr',  'nb-NO', 'Norwegian krone'],
  ['kr',  'da-DK', 'Danish krone'],
  ['Ft',  'hu-HU', 'Hungarian forint'],
  ['lei', 'ro-RO', 'Romanian leu'],
  ['₴',   'uk-UA', 'Ukrainian hryvnia'],
  ['$',   'en-CA', 'Canadian dollar'],
  ['$',   'en-AU', 'Australian dollar'],
];

function renderCurrencySample() {
  const symbol = document.getElementById('currency').value.trim() || 'zł';
  const locale = document.getElementById('locale').value.trim() || 'pl-PL';
  let shown;
  try {
    shown = (1234.5).toLocaleString(locale, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  } catch {
    shown = '—  (unknown format)';
  }
  document.getElementById('currency-sample').textContent = `e.g. ${shown} ${symbol}`;
}

function initCurrency() {
  const sel = document.getElementById('currency-preset');
  sel.innerHTML = CURRENCIES
    .map(([sym, loc, label], i) => `<option value="${i}">${esc(label)} — ${esc(sym)}</option>`)
    .join('') + '<option value="other">Other…</option>';

  sel.addEventListener('change', () => {
    if (sel.value === 'other') { renderCurrencySample(); return; }
    const [sym, loc] = CURRENCIES[Number(sel.value)];
    document.getElementById('currency').value = sym;
    document.getElementById('locale').value = loc;
    renderCurrencySample();
  });

  for (const id of ['currency', 'locale']) {
    document.getElementById(id).addEventListener('input', () => {
      sel.value = 'other';
      renderCurrencySample();
    });
  }
  renderCurrencySample();
}

initCurrency();

// ── wiring ────────────────────────────────────────────────────────────────────

for (const label of document.querySelectorAll('#presets label')) {
  label.addEventListener('click', async () => {
    for (const l of document.querySelectorAll('#presets label')) l.classList.remove('sel');
    label.classList.add('sel');
    label.querySelector('input').checked = true;
    state.preset = label.dataset.id || null;
    state.data = state.preset ? await api(`/api/setup/preset/${state.preset}`)
                              : { categories: [], income_categories: [], accounts: [] };
    renderTree();
    renderAccounts();
  });
}

document.getElementById('btn-add-acct').addEventListener('click',
  () => document.getElementById('acct-rows').appendChild(accountRow()));

document.getElementById('calc-on').addEventListener('change', e => {
  const box = document.getElementById('calc-config');
  box.style.display = e.target.checked ? '' : 'none';
  if (e.target.checked && !document.querySelector('#calc-rows .row')) {
    document.getElementById('calc-rows').appendChild(calcRow());
  }
});
document.getElementById('btn-add-calc').addEventListener('click',
  () => document.getElementById('calc-rows').appendChild(calcRow()));

document.getElementById('btn-finish').addEventListener('click', async () => {
  const err = document.getElementById('setup-err');
  const accounts = collectAccounts();
  if (!accounts.length) { err.textContent = 'Add at least one account.'; return; }
  err.textContent = '';

  const pct = (id) => {
    const v = document.getElementById(id).value.trim();
    return v === '' ? '' : String(parseFloat(v) / 100);
  };
  const calcItems = collectCalcItems();
  const settings = {
    currency: document.getElementById('currency').value.trim() || 'zł',
    locale: document.getElementById('locale').value.trim() || 'pl-PL',
    'calc.enabled': calcItems.length ? '1' : '0',
  };
  if (calcItems.length) {
    settings['calc.vat_rate'] = pct('calc-vat');
    settings['calc.tax_rate'] = pct('calc-tax');
    settings['calc.vat_label'] = document.getElementById('calc-vat-label').value.trim() || 'VAT';
    settings['calc.tax_label'] = document.getElementById('calc-tax-label').value.trim() || 'TAX';
  }

  const btn = document.getElementById('btn-finish');
  btn.disabled = true;
  const res = await api('/api/setup', {
    method: 'POST',
    body: JSON.stringify({
      preset: state.preset,
      categories: collectCategories(),
      income_categories: collectIncomes(),
      accounts,
      settings,
      calc_items: calcItems,
    }),
  });
  if (res && res.error) { err.textContent = res.error; btn.disabled = false; return; }
  window.location = '/';
});

// Start on the first preset so the page is never empty.
document.querySelector('#presets label').click();

}
