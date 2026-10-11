// Self-check for the offline write queue in mobile.js — no framework, plain asserts.
// Run: node tests/test_offline_queue.mjs
// Extracts and evaluates the queue-related functions with minimal DOM/fetch mocks,
// since mobile.js itself assumes a browser (document, localStorage, fetch, window).
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const src = readFileSync(new URL('../static/mobile.js', import.meta.url), 'utf8');

// Minimal browser shims
const store = {};
global.localStorage = {
  getItem: k => (k in store ? store[k] : null),
  setItem: (k, v) => { store[k] = String(v); },
};
const badgeEl = { textContent: '', style: { display: '' } };
global.document = {
  getElementById: id => (id === 'm-queue-badge' ? badgeEl : { classList: { contains: () => false } }),
  addEventListener: () => {},
};
global.window = { addEventListener: () => {} };
global.toast = () => {};

let fetchCalls = [];
let fetchBehavior = () => { throw new Error('offline'); };
// real fetch() always returns a Promise and rejects async on a network error —
// it never throws synchronously — so wrap fetchBehavior to match that contract.
global.fetch = (url, opts) => {
  try { return Promise.resolve(fetchBehavior(url, opts)); }
  catch (err) { return Promise.reject(err); }
};

// Stub out functions flushQueue calls at the end that aren't under test here.
global.loadAccounts = () => {};
global.loadBudget = () => Promise.resolve();
global.loadBudgetTab = () => Promise.resolve();
global.loadTxTab = () => {};

// Pull out the queue block plus api() rather than eval-ing the whole file, which has
// top-level DOM wiring this shim doesn't cover. Both markers live in mobile.js.
const start = src.indexOf('const QUEUE_KEY');
const end = src.indexOf('// END OFFLINE QUEUE');
assert.ok(start > -1 && end > start, 'could not locate queue block in mobile.js — did the code move?');
const queueSrc = src.slice(start, end);

const fn = new Function(`${queueSrc}\nreturn { loadQueue, saveQueue, queueWrite, flushQueue, api };`);
const { loadQueue, saveQueue, queueWrite, flushQueue, api } = fn();

// 1. queueWrite persists and returns a success-shaped response
const res = queueWrite('/api/transactions', { method: 'POST', body: '{"amount":5}' });
assert.deepEqual(res, { ok: true, queued: true });
assert.equal(loadQueue().length, 1);
assert.equal(badgeEl.style.display, '');
assert.equal(badgeEl.textContent, '1 QUEUED');

// 2. a second queued write stacks in order
queueWrite('/api/transactions', { method: 'POST', body: '{"amount":7}' });
assert.equal(loadQueue().length, 2);
assert.equal(loadQueue()[0].body, '{"amount":5}');
assert.equal(loadQueue()[1].body, '{"amount":7}');

// 3. flushQueue stops (keeps remaining items) on a network failure, preserving order
fetchBehavior = () => { throw new Error('still offline'); };
await flushQueue();
assert.equal(loadQueue().length, 2, 'items should survive a failed flush attempt');

// 4. flushQueue replays in order and drains the queue once back online
fetchCalls = [];
fetchBehavior = (url, opts) => { fetchCalls.push({ url, opts }); return Promise.resolve({ ok: true }); };
await flushQueue();
assert.equal(loadQueue().length, 0);
assert.equal(fetchCalls.length, 2);
assert.equal(fetchCalls[0].opts.body, '{"amount":5}');
assert.equal(badgeEl.style.display, 'none');

// 5. a server-rejected write (network OK, HTTP error) is dropped, not retried forever —
// and lands in the failed list so it isn't lost silently. flushQueue reads the body
// for the error message, so the shim has to answer json() like a real Response.
queueWrite('/api/transactions', { method: 'POST', body: '{"bad":true}' });
fetchBehavior = () => Promise.resolve({
  ok: false, status: 400, json: () => Promise.resolve({ error: 'Unknown category' }),
});
await flushQueue();
assert.equal(loadQueue().length, 0, 'a rejected write should be dropped, not stuck in the queue');
const failed = JSON.parse(store['mb-failed-writes'] || '[]');
assert.equal(failed.length, 1, 'a rejected write should be recorded as failed');
assert.equal(failed[0].reason, 'Unknown category');

// 6. noQueue: true writes fail fast offline instead of queuing. Balance
// corrections and BASIC compare against live state, so a late replay is unsafe.
fetchBehavior = () => { throw new Error('offline'); };
const balanceRes = await api('/api/accounts/7', { method: 'PUT', body: '{"balance":520}', noQueue: true });
assert.deepEqual(balanceRes, { error: 'offline' });
assert.equal(loadQueue().length, 0, 'a noQueue write must never land in the queue');

const basicRes = await api('/api/basic', {
  method: 'POST',
  body: '{"category_id":12,"amount":800,"expected_amount":700,"month":10,"year":2030}',
  noQueue: true,
});
assert.deepEqual(basicRes, { error: 'offline' });
assert.equal(loadQueue().length, 0, 'offline BASIC must not be queued');

fetchBehavior = () => Promise.resolve({
  ok: false,
  status: 409,
  json: () => Promise.resolve({
    error: 'BASIC changed since it was loaded.',
    current_amount: 725,
  }),
});
const conflictRes = await api('/api/basic', {
  method: 'POST', body: '{"amount":800,"expected_amount":700}', noQueue: true,
});
assert.deepEqual(conflictRes, {
  error: 'BASIC changed since it was loaded.', current_amount: 725, status: 409,
});
assert.equal(loadQueue().length, 0, 'a BASIC conflict must not be queued');

// Goal mapping depends on the current server-side IBAN decisions and account list.
// Replaying it later could target an account that was renamed, removed or remapped.
fetchBehavior = () => { throw new Error('offline'); };
const goalRes = await api('/api/transactions/42/goal-transfer', {
  method: 'POST', body: '{"account_id":7}', noQueue: true,
});
assert.deepEqual(goalRes, { error: 'offline' });
assert.equal(loadQueue().length, 0, 'offline goal mapping must not be queued');

// 7. ADDITIONAL changes are safe delayed writes. POST and DELETE both queue,
// retain their order, and replay FIFO when the phone reconnects.
const additionalPost = await api('/api/additional', {
  method: 'POST',
  body: '{"category_id":12,"amount":125,"description":"Shoes","month":10,"year":2030}',
});
const additionalDelete = await api('/api/additional/44', { method: 'DELETE' });
assert.equal(additionalPost.queued, true);
assert.equal(additionalDelete.queued, true);
assert.deepEqual(loadQueue().map(item => [item.method, item.url]), [
  ['POST', '/api/additional'],
  ['DELETE', '/api/additional/44'],
]);

fetchCalls = [];
fetchBehavior = (url, opts) => {
  fetchCalls.push({ url, opts });
  return Promise.resolve({ ok: true });
};
await flushQueue();
assert.equal(loadQueue().length, 0);
assert.deepEqual(fetchCalls.map(call => [call.opts.method, call.url]), [
  ['POST', '/api/additional'],
  ['DELETE', '/api/additional/44'],
]);

console.log('OK — all offline queue tests passed');
