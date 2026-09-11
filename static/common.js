/* Money Badger — shared frontend helpers.
   Loaded before every page script. These used to be copy-pasted into each of the
   eight per-tab files, which is how two esc() copies lost their quote-escaping and
   how the money formatter ended up with three different behaviours on null. */

const MONTHS = ['January','February','March','April','May','June',
                'July','August','September','October','November','December'];

/* Currency and locale come from the settings table, injected by each template
   just before this file loads. The fallback only matters if a page forgets to. */
const MB_SETTINGS = Object.assign({ currency: 'zł', locale: 'pl-PL' }, window.MB || {});

// ── formatting ────────────────────────────────────────────────────────────────

/* Always two decimals. Without them toLocaleString emits 0-3 fraction digits
   depending on the value, so a right-aligned money column renders "1 234 zł",
   "1 234,5 zł" and "1 234,56 zł" on adjacent rows and the decimal points don't
   line up — which is the entire point of right-aligning them. */
function fmtNum(n) {
  return Number(n || 0).toLocaleString(MB_SETTINGS.locale,
    { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + ' ' + MB_SETTINGS.currency;
}

/* Value for an <input type="number">: float noise like 1234.5600000000001 must not
   reach the field, because blur-saving writes it straight back to the database. */
function fmtAmountInput(n) {
  return (Math.round((Number(n) || 0) * 100) / 100).toFixed(2);
}

function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function formatDate(dateStr) {
  if (!dateStr) return '';
  if (/^\d{4}-\d{2}-\d{2}/.test(dateStr)) {
    const [y, m, dd] = dateStr.substring(0, 10).split('-');
    return `${dd}/${m}/${y}`;
  }
  if (/^\d{2}\/\d{2}\/\d{4}/.test(dateStr)) return dateStr.substring(0, 10);
  if (/^\d{2}\.\d{2}\.\d{4}/.test(dateStr)) {
    const [dd, m, y] = dateStr.substring(0, 10).split('.');
    return `${dd}/${m}/${y}`;
  }
  return dateStr;
}

/* Direction colour for a transaction amount. amount is stored positive for
   expenses, so the sign says nothing — tx_type plus the category side do. */
function amountColor(tx) {
  if (tx.tx_type === 'Money Transfer') return 'var(--blue)';
  if (tx.income_category_id || tx.tx_type === 'Income') return 'var(--green)';
  if (tx.tx_type === 'Balance Adjust') return tx.amount > 0 ? 'var(--green)' : 'var(--yellow)';
  return 'var(--yellow)';
}

// ── toast ─────────────────────────────────────────────────────────────────────

function toast(message, kind = 'ok') {
  let el = document.getElementById('mb-toast');
  if (!el) {
    el = document.createElement('div');
    el.id = 'mb-toast';
    el.setAttribute('role', 'status');
    el.setAttribute('aria-live', 'polite');
    document.body.appendChild(el);
  }
  el.textContent = message;
  el.className = `mb-toast mb-toast-${kind} visible`;
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.remove('visible'), kind === 'error' ? 5000 : 2000);
}

// ── api ───────────────────────────────────────────────────────────────────────

/* Every caller used to be `fetch(...).then(r => r.json())` with no r.ok check and
   no .catch. On a 500 Flask returned an HTML page, r.json() threw, init() aborted
   half-way and the page rendered an empty shell — indistinguishable from "all my
   data is gone". Errors now surface as a toast and reject, so callers stop. */
async function apiFetch(url, opts) {
  let res;
  try {
    res = await fetch(url, { headers: { 'Content-Type': 'application/json' }, ...opts });
  } catch (e) {
    toast('No connection to the server', 'error');
    throw e;
  }
  const body = await res.json().catch(() => null);
  if (!res.ok || (body && body.error)) {
    const msg = (body && body.error) || `Server error (${res.status})`;
    toast(msg, 'error');
    throw new Error(msg);
  }
  return body;
}

/* Pages that don't override it use the shared implementation directly. */
function api(url, opts) { return apiFetch(url, opts); }

// ── month navigation ──────────────────────────────────────────────────────────

/* Was copy-pasted five times, identically. */
function shiftMonth(state, delta) {
  state.month += delta;
  if (state.month < 1)  { state.month = 12; state.year--; }
  if (state.month > 12) { state.month = 1;  state.year++; }
}

// ── budget maths ──────────────────────────────────────────────────────────────

/* One implementation of the summary-bar numbers. EXPENSES and TRANSACTIONS used to
   compute this independently and disagreed: EXPENSES compared real bank+cash balance
   against what is still to be spent, TRANSACTIONS compared planned income against the
   whole month. The same month could read "SHORT 800 zł" on one tab and "funds
   available" on the other. */
function computeSummary(state) {
  let totalPlanned = 0, totalSpent = 0, remainingPlanned = 0;

  for (const g of state.categories) {
    const leaves = g.children.length ? g.children : [g];
    for (const cat of leaves) {
      const adds = (state.budget.additionals[String(cat.id)]
                 || state.budget.additionals[cat.id] || []).reduce((s, a) => s + a.amount, 0);
      const plan  = (cat.basic || 0) + adds;
      const spent = Math.abs(state.budget.spent[String(cat.id)] ?? state.budget.spent[cat.id] ?? 0);
      totalPlanned     += plan;
      totalSpent       += spent;
      remainingPlanned += Math.max(0, plan - spent);
    }
    // spending booked directly on a parent group (the pipeline can produce it)
    if (g.children.length) {
      totalSpent += Math.abs(state.budget.spent[String(g.id)] ?? state.budget.spent[g.id] ?? 0);
    }
  }

  const income = state.budget.income || 0;
  // The account balance already reflects what has been spent, so it only has to
  // cover what is still left to spend, plus income expected but not yet landed.
  const incomeUpcoming = Math.max(0, income - (state.budget.incomeReceived || 0));
  const realAvailable  = (state.budget.availableCash || 0) + incomeUpcoming;

  return { totalPlanned, totalSpent, remainingPlanned, income, realAvailable };
}

/* Paints the four summary-bar slots. Both pages call this with the same state shape. */
function renderSummary(state) {
  const s = computeSummary(state);

  const incomeEl = document.getElementById('sum-income');
  if (incomeEl) incomeEl.textContent = s.income > 0 ? fmtNum(s.income) : '—';

  const plannedEl = document.getElementById('sum-planned');
  if (plannedEl) {
    plannedEl.textContent = fmtNum(s.totalPlanned);
    plannedEl.style.color = s.income > 0
      ? (s.totalPlanned <= s.income ? 'var(--green)' : 'var(--red)')
      : 'var(--yellow)';
  }

  const spentEl = document.getElementById('sum-spent');
  if (spentEl) spentEl.textContent = fmtNum(s.totalSpent);

  const el = document.getElementById('sum-narrative');
  if (!el) return;
  if (s.totalSpent <= s.totalPlanned) {
    const toSpend = s.income > 0 ? s.income - s.totalSpent : s.totalPlanned - s.totalSpent;
    el.innerHTML = `TO SPEND: <strong style="color:var(--green)">${fmtNum(toSpend)}</strong>`;
  } else if (s.realAvailable >= s.remainingPlanned) {
    const over = s.totalSpent - s.totalPlanned;
    el.innerHTML = `Over budget by <strong style="color:var(--yellow)">${fmtNum(over)}</strong> — funds available`;
  } else {
    const shortage = s.remainingPlanned - s.realAvailable;
    el.innerHTML = `SHORT <strong style="color:var(--red)">${fmtNum(shortage)}</strong> to cover expenses`;
  }
}

/* Bank/cash total that the summary compares against. Asset accounts (savings, goals)
   are deliberately excluded — they are not money available for this month's overspend. */
function availableCashFrom(accounts) {
  return (accounts || [])
    .filter(a => a.type === 'bank' || a.type === 'cash')
    .reduce((s, a) => s + (a.balance || 0), 0);
}

/* The two balance bars (accounts / assets), previously three near-identical copies. */
function renderBalanceBars(accounts) {
  const accEl   = document.getElementById('balance-items-accounts');
  const assetEl = document.getElementById('balance-items-assets');
  if (!accEl || !assetEl) return;
  accEl.innerHTML   = '';
  assetEl.innerHTML = '';

  for (const acct of accounts) {
    const isAsset = acct.type === 'asset';
    const item = document.createElement('div');
    item.className = 'balance-item';
    item.innerHTML = `
      <span class="balance-name">${esc(acct.name)}</span>
      <span class="${isAsset ? 'balance-val balance-val-teal' : 'balance-val'}"
            style="${acct.balance < 0 ? 'color:var(--red)' : ''}">${fmtNum(acct.balance)}</span>
    `;
    (isAsset ? assetEl : accEl).appendChild(item);
  }
}

// ── keyboard access ───────────────────────────────────────────────────────────

/* Rows and divs with a click handler are invisible to a keyboard: no tab stop, no
   Enter/Space. The whole budget tree — expanding a group, opening the edit modal,
   correcting a balance — was mouse-only. This makes one of them behave like a button. */
function clickable(el, handler, label) {
  el.tabIndex = 0;
  el.setAttribute('role', 'button');
  if (label) el.setAttribute('aria-label', label);
  el.addEventListener('click', handler);
  el.addEventListener('keydown', e => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); handler(e); }
  });
}

// Card-payment descriptions come from EB as "VISA PLAT <card> PŁATNOŚĆ KARTĄ <amount> PLN  <merchant>" —
// merchant (the only useful part on a quick scan) is buried at the end. Same shape for ZWROT PŁATNOŚCI /
// PRZELEW / WYPŁATA Z BANKOMATU KARTĄ and for foreign currency ("8.10 GBP 1 GBP=5.0590 PLN").
// Cosmetic reorder for display only; the raw string stays untouched in the DB and is what an edit
// field shows while editing.
const CARD_TX_RE = /^((?:DOP\.\s*)?(?:VISA|MASTERCARD)\s*(?:PLAT)?\s*[\d*]{4,}\s+[A-ZĄĆĘŁŃÓŚŹŻ ]*?KART[ĄA]\s*[\d.,]+\s*[A-Z]{3}(?:\s+1\s+[A-Z]{3}=[\d.,]+\s*PLN)?)\s+(.+)$/;

function fmtDesc(desc) {
  if (!desc) return desc;
  const m = desc.match(CARD_TX_RE);
  return m ? `${m[2].trim()} | ${m[1].trim()}` : desc;
}
