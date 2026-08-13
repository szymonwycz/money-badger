// Money Badger — yearly OVERVIEW: planned/spent bars + income line, category matrix.
const MONTHS_SHORT = ['JAN','FEB','MAR','APR','MAY','JUN','JUL','AUG','SEP','OCT','NOV','DEC'];

const state = { year: INIT_YEAR, data: null };

function fmtShort(n) {
  if (Math.abs(n) >= 1000) return (n / 1000).toLocaleString(MB_SETTINGS.locale, {maximumFractionDigits: 1}) + 'k';
  return String(Math.round(n));
}

// ── tooltip ──────────────────────────────────────────────────────────────────

const tt = document.getElementById('ov-tooltip');
function showTip(evt, month) {
  tt.innerHTML = `
    <div class="tt-title">${MONTHS[month.month - 1]} ${state.year}</div>
    <div class="tt-row"><span>Planned</span><b>${fmtNum(month.planned)}</b></div>
    <div class="tt-row"><span>Spent</span><b>${fmtNum(month.spent)}</b></div>
    <div class="tt-row"><span>Income received</span><b>${fmtNum(month.income_received)}</b></div>
    <div class="tt-row"><span>Income planned</span><b>${fmtNum(month.income_planned)}</b></div>`;
  tt.style.display = 'block';
  positionTip(evt);
}
function positionTip(evt) {
  const pad = 14;
  let x = evt.clientX + pad, y = evt.clientY + pad;
  const r = tt.getBoundingClientRect();
  if (x + r.width > window.innerWidth - 8) x = evt.clientX - r.width - pad;
  if (y + r.height > window.innerHeight - 8) y = evt.clientY - r.height - pad;
  tt.style.left = x + 'px'; tt.style.top = y + 'px';
}
function hideTip() { tt.style.display = 'none'; }

// ── chart (hand-rolled SVG: grouped bars + income line) ──────────────────────

function renderChart() {
  const months = state.data.months;
  const W = 980, H = 300, padL = 56, padR = 16, padT = 18, padB = 28;
  const plotW = W - padL - padR, plotH = H - padT - padB;

  const maxVal = Math.max(1,
    ...months.map(m => Math.max(m.planned, m.spent, m.income_received)));
  const niceMax = Math.ceil(maxVal / 5000) * 5000 || 5000;
  const y = v => padT + plotH - (v / niceMax) * plotH;

  const slotW  = plotW / 12;
  const barW   = Math.min(16, (slotW - 14) / 2);
  const gap    = 2;                       // surface gap between the pair

  let svg = `<svg viewBox="0 0 ${W} ${H}" width="100%" style="min-width:760px" font-family="inherit">`;

  // gridlines + y labels (recessive)
  const steps = 5;
  for (let i = 0; i <= steps; i++) {
    const v = niceMax / steps * i, yy = y(v);
    svg += `<line x1="${padL}" y1="${yy}" x2="${W - padR}" y2="${yy}" stroke="var(--border)" stroke-width="1"/>`;
    svg += `<text x="${padL - 8}" y="${yy + 4}" text-anchor="end" font-size="10" fill="var(--text2)">${fmtShort(v)}</text>`;
  }

  // bars
  months.forEach((m, i) => {
    const cx = padL + slotW * i + slotW / 2;
    const x1 = cx - barW - gap / 2, x2 = cx + gap / 2;
    if (m.planned > 0) {
      svg += `<rect data-mi="${i}" x="${x1}" y="${y(m.planned)}" width="${barW}" height="${Math.max(0, y(0) - y(m.planned))}"
               rx="3" fill="var(--blue)"/>`;
    }
    if (m.spent > 0) {
      svg += `<rect data-mi="${i}" x="${x2}" y="${y(m.spent)}" width="${barW}" height="${Math.max(0, y(0) - y(m.spent))}"
               rx="3" fill="var(--yellow)"/>`;
    }
    // invisible hover target covering the whole month slot
    svg += `<rect data-mi="${i}" x="${padL + slotW * i}" y="${padT}" width="${slotW}" height="${plotH}" fill="transparent"/>`;
    svg += `<text x="${cx}" y="${H - 10}" text-anchor="middle" font-size="10" fill="var(--text2)">${MONTHS_SHORT[i]}</text>`;
  });

  // income line (distinct mark type + direct label — readable without color)
  const pts = months.map((m, i) => {
    const cx = padL + slotW * i + slotW / 2;
    return { x: cx, y: y(m.income_received), v: m.income_received };
  });
  const lastNonZero = pts.map((p, i) => ({...p, i})).filter(p => p.v > 0).pop();
  svg += `<polyline points="${pts.map(p => `${p.x},${p.y}`).join(' ')}"
           fill="none" stroke="var(--green)" stroke-width="2"/>`;
  for (const p of pts) {
    if (p.v > 0) {
      svg += `<circle cx="${p.x}" cy="${p.y}" r="4" fill="var(--green)" stroke="var(--panel)" stroke-width="2"/>`;
    }
  }
  if (lastNonZero) {
    svg += `<text x="${lastNonZero.x + 8}" y="${lastNonZero.y - 8}" font-size="10" font-weight="700"
             fill="var(--green)">INCOME</text>`;
  }

  svg += '</svg>';
  const el = document.getElementById('ov-chart');
  el.innerHTML = svg;

  el.querySelectorAll('[data-mi]').forEach(node => {
    const m = months[parseInt(node.dataset.mi)];
    node.addEventListener('mousemove', e => showTip(e, m));
    node.addEventListener('mouseleave', hideTip);
  });
}

// ── category × month table with sequential shading ───────────────────────────

function renderTable() {
  const cats = state.data.categories;
  const table = document.getElementById('ov-table');
  if (!cats.length) {
    table.innerHTML = '<tr><td style="color:var(--text2)">No spending recorded this year.</td></tr>';
    return;
  }
  const cellMax = Math.max(...cats.flatMap(c => c.monthly));

  let html = '<tr><th>CATEGORY</th>' +
    MONTHS_SHORT.map(m => `<th>${m}</th>`).join('') + '<th class="tot">TOTAL</th></tr>';

  for (const c of cats) {
    html += `<tr><td>${esc(c.name)}</td>`;
    c.monthly.forEach(v => {
      // sequential single-hue shading; alpha capped so light text stays readable
      const alpha = cellMax > 0 ? Math.min(0.45, (v / cellMax) * 0.45) : 0;
      html += `<td style="background:rgba(232,184,75,${alpha.toFixed(3)})">${v > 0 ? fmtShort(v) : ''}</td>`;
    });
    html += `<td class="tot">${fmtNum(c.total)}</td></tr>`;
  }

  // totals row
  const monthTotals = MONTHS_SHORT.map((_, i) => cats.reduce((s, c) => s + c.monthly[i], 0));
  html += `<tr><td style="font-weight:700;border-top:1px solid var(--border)">TOTAL</td>` +
    monthTotals.map(v => `<td style="font-weight:700;border-top:1px solid var(--border)">${v > 0 ? fmtShort(v) : ''}</td>`).join('') +
    `<td class="tot" style="border-top:1px solid var(--border)">${fmtNum(monthTotals.reduce((a, b) => a + b, 0))}</td></tr>`;

  table.innerHTML = html;
}

// ── load / nav ───────────────────────────────────────────────────────────────

async function load() {
  document.getElementById('year-label').textContent = state.year;
  state.data = await api(`/api/overview?year=${state.year}`);
  renderChart();
  renderTable();
}

document.getElementById('btn-prev').addEventListener('click', () => { state.year--; load(); });
document.getElementById('btn-next').addEventListener('click', () => { state.year++; load(); });

document.getElementById('btn-export-tx').addEventListener('click', () => {
  window.location = `/api/export/transactions.csv?year=${state.year}`;
});
document.getElementById('btn-export-budget').addEventListener('click', () => {
  window.location = `/api/export/budget.csv?year=${state.year}`;
});

load();
