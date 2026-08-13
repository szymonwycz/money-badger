# Money Badger — API Reference

All endpoints are JSON unless noted. `month` is 1–12; amounts are floats in
whatever currency the `currency` setting names. Common error shape:
`{"error": "message"}` with status 400/404.

## Authentication

When `MB_PASSWORD_HASH` is set, everything below requires either a session
cookie from `POST /login` or an `X-MB-Token` header matching `MB_API_TOKEN`.
`/api/*` answers **401** when unauthenticated rather than redirecting, so a
fetch() gets a real error instead of an HTML login page it can't parse.

With `MB_PASSWORD_HASH` empty there is no authentication at all.

Logins are per person and the budget is shared: an account decides who a
transaction is attributed to, never what is visible. `MB_PASSWORD_HASH` becomes
the first admin account on startup — named by `MB_ADMIN_USER`, default `admin` —
after which the hashes live in the `users` table and the variable is only the
on/off switch. `X-MB-Token` is the pipeline, not a person: rows it creates have
no author.

| Endpoint | Description |
|---|---|
| `GET /login` | Login form |
| `POST /login` | `username=` + `password=` form fields. `?next=` is honoured only for in-app paths |
| `GET /logout` | Clears the session |

## Pages

| Route | Page |
|---|---|
| `GET /` | EXPENSES |
| `GET /income` | INCOME |
| `GET /check-me` | CHECK ME |
| `GET /transactions` | TRANSACTIONS |
| `GET /large-expenses` | LARGE EXPENSES |
| `GET /overview` | OVERVIEW (yearly) |
| `GET /calculator` | CALCULATOR (only when `calc.enabled`) |
| `GET /account` | ACCOUNT — your own password, plus the people list for an admin |
| `GET /m` | Mobile PWA |
| `GET /sw.js` | Service worker (root scope) |
| `GET /setup` | First-run setup; every other page redirects here until it's done |

## Categories (expense)

| Endpoint | Description |
|---|---|
| `GET /api/categories?month=&year=` | Category tree with effective per-month BASIC. Groups and children sorted A–Z |
| `POST /api/categories` | `{name, parent_id?}` — add group (no parent) or subcategory |
| `DELETE /api/categories/<id>` | 400 if it has subcategories or attached data |

## Budget (EXPENSES)

| Endpoint | Description |
|---|---|
| `GET /api/budget?month=&year=` | `{additionals: {cat_id: [{id, amount, description}]}, spent: {cat_id: total}, income}` — income falls back to legacy `monthly_income` when no `income_entries` exist |
| `POST /api/basic` | `{category_id, amount, month, year}` — upsert per-month BASIC |
| `POST /api/basic/copy-next` | `{month, year}` — copy effective BASIC to month+1 |
| `POST /api/additional` | `{category_id, amount, description?, month, year}` |
| `DELETE /api/additional/<id>` | |

## Transactions

| Endpoint | Description |
|---|---|
| `GET /api/transactions?month=&year=` | Month's transactions with resolved category names; transfer descriptions rendered as `→ TargetAccount`. `created_by` is the username that entered it, `null` for imports and for anything older than accounts |
| `POST /api/transactions` | Manual add: `{date, amount, account?, description?, category_id?, tx_type?}`. Date accepts `DD/MM/YYYY`, `YYYY-MM-DD`, `DD.MM.YYYY`; stored as ISO. Attributed to the signed-in account |
| `POST /api/transactions/bulk` | Pipeline import. Array of `{date, amount, account, account_to?, tx_type?, description?, category, hash}`. Hash-deduplicated (`INSERT OR IGNORE`). Resolves `category` against expense tree first, then income categories; the literal `CHECK ME` is never resolved. Income inserts credit `income_entries.received` |
| `PUT /api/transactions/<id>` | Partial update of `date/amount/account/description/category_id`. Category changes are logged to `corrections`; amount/month changes on income transactions adjust `received`. Invalid date → 400 |
| `DELETE /api/transactions/<id>` | Deletes (rolls back `received` for income, removes its correction rows) |
| `PUT /api/transactions/<id>/category` | Assign expense category (CHECK ME flow), logs a correction |
| `PUT /api/transactions/<id>/income-category` | Assign income category, credits `received` (moves it if reassigned) |
| `GET /api/transactions/unreviewed` | CHECK ME inbox, all-time: `{expenses: [...], income: [...]}` bucketed by `tx_type` |
| `GET /api/transactions/unreviewed/count` | `{count}` — powers the nav badge |

## Accounts

| Endpoint | Description |
|---|---|
| `GET /api/accounts` | Accounts with **computed** balances (checkpoint + transactions since anchor) |
| `POST /api/accounts` | `{name, type}` — type: `bank` / `cash` / `asset` |
| `PUT /api/accounts/<id>` | `{balance}` — manual balance edit: writes a `Balance Adjust` audit row and re-anchors the checkpoint |
| `DELETE /api/accounts/<id>` | |
| `GET /api/accounts/last-dates` | `{account_name: "YYYY-MM-DD"}` last transaction date per account (pipeline resume fallback) |

## Income

| Endpoint | Description |
|---|---|
| `GET /api/income-categories` / `POST` / `DELETE /<id>` | DELETE → 400 when transactions reference the category |
| `GET /api/income-entries?month=&year=` | One row per category (zeros when absent) |
| `PUT /api/income-entries` | Upsert `{category_id, month, year, planned, received, note}` |
| `POST /api/income-entries/copy-next` | Copy `planned`+`note` to month+1; never overwrites `received` |

## Large expenses

| Endpoint | Description |
|---|---|
| `GET /api/big-expenses?year=` / `GET /api/big-expenses/all` | Flat list / grouped per year with totals |
| `POST /api/big-expenses` | `{description, amount, year, planned_month?}` |
| `PUT /api/big-expenses/<id>` | `{description, amount, done, planned_month}` |
| `DELETE /api/big-expenses/<id>` | |

## Overview & export

| Endpoint | Description |
|---|---|
| `GET /api/overview?year=` | `{year, months: [{month, planned, spent, income_planned, income_received}×12], categories: [{id, name, monthly[12], total}]}`. Spending rolled up to parent groups; transfers/adjusts excluded |
| `GET /api/export/transactions.csv?year=&month=` | Semicolon CSV of transactions (filters optional) |
| `GET /api/export/budget.csv?year=` | Semicolon CSV: per month/category Basic;Additional;Planned;Spent |

## Settings

| Endpoint | Description |
|---|---|
| `GET /api/settings` | All settings as a flat `{key: value}` map |
| `PUT /api/settings` | Upsert the given keys; returns the full map |

See [CONFIGURATION.md](CONFIGURATION.md#the-settings-table) for the keys.

## People

Admin only, and there is no admin on an install with the login switched off — so
every one of these answers **403** there.

| Endpoint | Description |
|---|---|
| `GET /api/users` | `[{id, username, is_admin, created_at}]` — never a hash |
| `POST /api/users` | `{username, password, is_admin?}`. Username is 2–32 of `A-Za-z0-9._-` and case-insensitively unique; password at least 8 characters. **409** if the name is taken |
| `DELETE /api/users/<id>` | Removes the login; their transactions stay and lose their author. **400** on your own account, which is what keeps one admin standing |
| `PUT /api/users/<id>/password` | `{password}` — reset a forgotten one. **400** on your own account |
| `PUT /api/account/password` | `{current_password, password}` — your own, for any signed-in user (not admin-only). **403** if the current one is wrong |

## Setup

Only useful on an unconfigured install.

| Endpoint | Description |
|---|---|
| `GET /api/setup/preset/<id>` | A preset's categories, income categories and accounts |
| `POST /api/setup` | `{preset, categories, income_categories, accounts, settings, calc_items}` — applies everything in one transaction. **409** if the install is already configured |

## Calculator

Off by default; `calc.enabled` turns it on.

| Endpoint | Description |
|---|---|
| `GET /api/calc?month=&year=` | `{settings, items, adjustments, subtotal, vat, tax, gross}`. `vat`/`tax` are `null` when the rate isn't configured — distinct from `0` |
| `POST /api/calc/items` | `{name, unit?, time_input?, rate?}` — define a line item |
| `PUT /api/calc/items/<id>` | Rename, re-unit or re-rate one |
| `DELETE /api/calc/items/<id>` | Removes the item with its saved months and adjustments |
| `PUT /api/calc/plan` | `{month, year, entries: [{item_id, qty, rate}]}` — this month's quantities |
| `POST /api/calc/adjustment` | `{month, year, item_id, sign, qty, description?}` |
| `DELETE /api/calc/adjustment/<id>` | |
| `POST /api/calc/apply` | `{month, year}` → gross as planned income in `calc.income_category`, VAT and tax as additional budgets. **400** naming any configured category that doesn't exist |

## Corrections loop

| Endpoint | Description |
|---|---|
| `GET /api/corrections?synced=0` | Unsynced category corrections as CSV. The `X-Correction-Ids` response header lists exactly which rows it covers, so only those are marked later |
| `POST /api/corrections/mark-synced` | Mark all as synced (called after rule learning) |
