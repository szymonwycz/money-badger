// Self-check for shared Expenses/Transactions and Income summary bars.
// Run: node tests/test_summary.mjs
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const src = readFileSync(new URL('../static/common.js', import.meta.url), 'utf8');
const css = readFileSync(new URL('../static/style.css', import.meta.url), 'utf8');
const incomeSrc = readFileSync(new URL('../static/income.js', import.meta.url), 'utf8');
const templates = [
  '../templates/index.html',
  '../templates/transactions.html',
  '../templates/income.html',
].map(path => readFileSync(new URL(path, import.meta.url), 'utf8'));

const start = src.indexOf('function computeSummary');
const end = src.indexOf('/* Bank/cash total');
assert.ok(start > -1 && end > start, 'could not locate summary functions');

const elements = Object.fromEntries(
  ['sum-income', 'sum-planned', 'sum-spent', 'sum-narrative'].map(id => [
    id, { textContent: '', innerHTML: '', className: '', style: {} },
  ]),
);
const document = { getElementById: id => elements[id] || null };
const fmtNum = n => `${Number(n || 0).toFixed(2)} EUR`;
const loadSummary = new Function(
  'document', 'fmtNum',
  `${src.slice(start, end)}\nreturn { computeSummary, renderSummary };`,
);
const { computeSummary, renderSummary } = loadSummary(document, fmtNum);
const compact = html => html.replace(/\s+/g, ' ').trim();

function stateWith({ planned, spent, availableCash = 0, incomeUpcoming, income = 0 }) {
  return {
    categories: planned.map((basic, index) => ({ id: index + 1, basic, children: [] })),
    budget: {
      additionals: {},
      spent: Object.fromEntries(spent.map((amount, index) => [index + 1, amount])),
      income,
      incomeUpcoming,
      availableCash,
    },
  };
}

const covered = stateWith({
  planned: [0, 700],
  spent: [1000, 0],
  availableCash: 500,
  incomeUpcoming: 300,
});
const totals = computeSummary(covered);
assert.equal(totals.realAvailable - totals.remainingPlanned, 100);
renderSummary(covered);
assert.equal(
  compact(elements['sum-narrative'].innerHTML),
  '<div class="sum-block"> <span class="sum-label">SPENT OVER PLAN</span> <span class="sum-val">300.00 EUR</span> </div> <div class="sum-divider"></div> <div class="sum-block"> <span class="sum-label">BUFFER</span> <span class="sum-val sum-income">+ 100.00 EUR</span> </div>',
);
assert.equal(elements['sum-narrative'].className, 'summary-status');

renderSummary(stateWith({ planned: [1000], spent: [400], income: 1200 }));
assert.equal(
  compact(elements['sum-narrative'].innerHTML),
  '<div class="sum-block"> <span class="sum-label">TO SPEND</span> <span class="sum-val sum-income">800.00 EUR</span> </div>',
);

renderSummary(stateWith({
  planned: [0, 700],
  spent: [1000, 0],
  availableCash: 200,
  incomeUpcoming: 100,
}));
assert.equal(
  compact(elements['sum-narrative'].innerHTML),
  '<div class="sum-block"> <span class="sum-label">SPENT OVER PLAN</span> <span class="sum-val">300.00 EUR</span> </div> <div class="sum-divider"></div> <div class="sum-block"> <span class="sum-label">BUFFER</span> <span class="sum-val sum-negative">- 400.00 EUR</span> </div>',
);

const incomeStart = incomeSrc.indexOf('function updateSummary');
const incomeEnd = incomeSrc.indexOf('// ── table');
assert.ok(incomeStart > -1 && incomeEnd > incomeStart, 'could not locate income summary');
const incomeElements = Object.fromEntries(
  ['sum-planned', 'sum-received', 'sum-remaining', 'sum-narrative'].map(id => [
    id, { textContent: '', innerHTML: '', className: '', style: {} },
  ]),
);
const incomeDocument = { getElementById: id => incomeElements[id] || null };
const loadIncomeSummary = new Function(
  'document', 'fmtNum', 'entries',
  `const state = { entries }; ${incomeSrc.slice(incomeStart, incomeEnd)}\nreturn updateSummary;`,
);
loadIncomeSummary(incomeDocument, fmtNum, [
  { name: 'Primary income', planned: 1000, received: 400 },
  { name: 'Reimbursement', planned: 0, received: 300 },
])();
assert.equal(incomeElements['sum-remaining'].textContent, '600.00 EUR');
assert.equal(incomeElements['sum-remaining'].textContent.includes('short'), false);
assert.equal(
  compact(incomeElements['sum-narrative'].innerHTML),
  '<div class="sum-block"> <span class="sum-label">EXTRA RECEIVED</span> <span class="sum-val sum-income">300.00 EUR</span> </div>',
);

assert.match(css, /\.summary-status\s*\{\s*display:\s*contents;\s*\}/s);
assert.match(
  css,
  /@media\s*\(max-width:\s*760px\)\s*\{[\s\S]*?\.summary-bar\s*\{[^}]*grid-template-columns:\s*repeat\(2,\s*minmax\(0,\s*1fr\)\);/s,
);
assert.match(css, /@media\s*\(max-width:\s*760px\)\s*\{[\s\S]*?\.app-logo\s*\{\s*display:\s*none;/s);
assert.doesNotMatch(css, /#sum-narrative/);
for (const template of templates) {
  assert.match(template, /class="summary-status" id="sum-narrative"/);
}

console.log('OK — summary math, copy and responsive rules passed');
