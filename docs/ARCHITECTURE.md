# Money Badger — Architecture

## Overview

Money Badger is a deliberately small system: one Flask process, one SQLite
file, static-file frontend with vanilla JavaScript. There is no build step, no
frontend framework, and no ORM. Every architectural decision below optimizes
for a household app that must be trivially operable on a Raspberry Pi for
years, by the person who installed it and nobody else.

```
                    ┌──────────────────────────────────────────┐
   browser / PWA    │ one small server (a Pi is plenty)        │
   over LAN or VPN  │                                          │
        │           │  [reverse proxy] ──► gunicorn ──► Flask  │
        └───────────►                          │               │
                    │                          ▼               │
                    │           SQLite budget.db (WAL)         │
                    │                          ▲               │
                    │  sync timer (daily, optional)            │
                    │  ├─ bank fetch (Enable Banking)          │
                    │  ├─ filter, keyword rules, then an LLM   │
                    │  └─ HTTP POST back into the same API ────┘
                    └──────────────────────────────────────────┘
```

## Components

### Backend — `app.py`

Single-file Flask application. Routes are grouped by domain (categories,
budget, transactions, accounts, income, overview, calculator). JSON in,
JSON out; the only non-JSON responses are CSV exports and the corrections
corrections feed.

Database access is a per-request `sqlite3` connection (`g.db`) with foreign
keys enforced and a 5 s busy timeout. There is no ORM — queries are short,
hand-written SQL close to where they're used.

### Schema migrations — `migrations.py`

Versioned, forward-only migrations recorded in `schema_migrations`. Rules:

- **Never edit a shipped migration** — append a new one.
- Migration `0001_baseline` is idempotent because production databases predate
  the migration system; its first run against an existing database is a no-op.
- Run automatically on app startup, or manually:
  `python migrations.py [path-to-db]`.

### Frontend — `templates/` + `static/`

One HTML template and one JS file per tab. Vanilla JS with `fetch`; shared
conventions (state object, `api()`, `esc()`, `fmtNum()`) are repeated per file
rather than abstracted — files are ~200–500 lines and independent, so a page
can be understood in isolation. Visual language is defined in
`design_system.md` and `static/design_tokens.css`.

### Mobile PWA — `/m`

A separate, phone-shaped view over the same API: quick expense entry (with
last-used category/account remembered in `localStorage`), balances, and budget
status. Installable via web manifest; a minimal service worker caches the
static shell only — **API responses are never cached** (financial data must be
live).

## Data model

| Table | Purpose |
|---|---|
| `categories` | Expense category tree, one level deep (`parent_id` NULL = group) |
| `basic_budgets` | Legacy global BASIC per category (fallback) |
| `basic_budgets_monthly` | BASIC per category per month — overrides the global value |
| `additional_budgets` | One-off budget entries for a month, with notes |
| `transactions` | All money movements; see "transaction semantics" below |
| `corrections` | Category corrections queued for the rule-learning loop |
| `accounts` | Accounts with checkpoint balance + `balance_anchor_tx_id` |
| `income_categories` / `income_entries` | Income plan/received per category per month |
| `monthly_income` | Legacy single-number income (fallback only) |
| `big_expenses` | Yearly one-off projects (LARGE EXPENSES tab) |
| `calc_items` / `calc_entries` / `calc_adjustments` | Calculator: item definitions, monthly quantities, adjustments |
| `settings` | Currency, locale, calculator wiring |
| `users` | Who can sign in: username, password hash, admin flag |
| `schema_migrations` | Applied migration log |

### Transaction semantics (important)

- `amount` is stored **positive for expenses** — direction is carried by
  `tx_type` (`Expense`, `Income`, `Money Transfer`, `Balance Adjust`) and by
  which category field is set (`category_id` = expense, `income_category_id` =
  income). Never infer direction from the sign.
- `source_hash` (UNIQUE) deduplicates pipeline imports; manual transactions
  have no hash.
- `Balance Adjust` rows are **audit entries** for manual balance edits — they
  are excluded from balance computation and budget math.
- `created_by` is the account that entered the row by hand. Pipeline imports and
  everything from before accounts existed have none, and removing a user clears it
  rather than deleting their money.

### Computed balances

`accounts.balance` is a *checkpoint*, not a live value. The displayed balance
is:

```
balance + Σ effect(tx)  for tx.id > balance_anchor_tx_id
```

where `effect` is +amount for income, −amount for expenses, and ± for
transfers touching the account. Manually editing a balance writes a
`Balance Adjust` audit row and moves the anchor to it, so history before the
edit no longer affects the display. The daily Enable Banking balance fetch
does **not** overwrite balances — it only warns on drift (a drift signals a
missing or duplicated transaction, which should be fixed, not papered over).

### Income received

Imported income transactions credit `income_entries.received` at insert time
(`bump_income_received`). Edits and deletes of income transactions adjust it
by the delta, so the INCOME tab stays consistent with the transaction list.

## Integration: bank import pipeline

Money Badger does not talk to banks itself. A separate pipeline (same repo,
`sync/`, orchestrated by `master_pi.py` under `money-badger-sync.timer`):

1. fetches transactions via Enable Banking (PSD2 API),
2. categorizes them (rules first, LLM for the rest),
3. matches Allegro purchases via Gmail IMAP,
4. pushes to `/api/transactions/bulk` (hash-deduplicated),
5. pulls category corrections back for rule learning,
6. compares bank balances against computed balances (warn-only).

Anything unmatched lands in **CHECK ME** for manual review.

## Design decisions

- **SQLite over a client-server database.** Single-writer household workload,
  one gunicorn worker; WAL mode lets backups read concurrently. The schema
  uses plain SQL92 + `ON CONFLICT` upserts, so a future Postgres port is
  mechanical (see migrations).
- **No frontend framework.** Server templates + vanilla JS keep the deploy a
  `git pull` + service restart, with zero toolchain on the Pi.
- **English UI, your data in your language.** Interface text is English; category
  and account names are user content and stay exactly as typed.
- **One currency per install.** Symbol and number format are settings, injected
  into every page. Multi-currency — holding balances in two at once, with rates —
  is a different feature and is out of scope.
- **Separate logins, one shared budget.** Everybody signs in as themselves and
  sees the same money: no table is scoped to a user, and `transactions.created_by`
  only records who typed a row in. Keeping households apart in one database is a
  different product; the schema leaves the door open — people carry a surrogate id,
  so a `household_id` column later lands on `users` and on the data tables without
  rewriting what points at them — but nothing implements it. `MB_PASSWORD_HASH`
  bootstraps the first admin account and can still be left empty to switch the
  login off entirely for a VPN-only install.
- **Nothing seeded.** Migrations build the schema and stop. Starter data comes from
  a preset chosen at `/setup`, so no install inherits anyone else's categories.
