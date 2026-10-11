# Changelog

Notable changes to Money Badger. Dates are Europe/Warsaw.

## Unreleased

### Fixed
- **Intraday bank passes now fetch the open current day safely.** Every pass asks
  through today with a two-day overlap, while the durable checkpoint is clamped
  to yesterday. Explicit `PDNG` rows wait until booked; missing/unknown statuses
  remain importable. A catch-up gap beyond `days_back` is reported in the run
  summary and Telegram without misclassifying the account as failed.
- **Later sync passes no longer overwrite the day's earlier CSV.** Each account's
  daily outbox is merged by the Enable Banking id stored in `Check #` (split rows
  use `#0`, `#1`, ...), then atomically replaced before the fetch checkpoint is
  committed. CSV fields are serialized with `csv.writer`, so semicolons, quotes
  or line breaks in bank/LLM text cannot shift the transaction type or category.
- **Bulk import rejects unknown transaction types per row.** The valid remainder
  of the batch is still accepted, while the malformed row's hash stays unaccepted
  so the sender continues to report it instead of storing balance-corrupting data.
- **Rule learning rejects unsafe generalizations before writing `rules.json`.** It
  never learns CHECK ME, accepts only categories and literal fragments supported
  by the input corrections, rejects malformed rules and generic card-operation
  wording in English, Polish and other common European languages, and logs every
  rejection. Optional `account_holders` still blocks holder-name-only rules.
- **Two pipelines could run at once.** Both sync timers carry `Persistent=true`, so
  a machine that was off at the scheduled time replays the missed sync and the
  missed watchdog in the same second — and the watchdog, seeing no success marker
  yet, starts a second pipeline beside the running one: two bank fetches out of one
  daily quota, two pushes, two order-matching runs overwriting each other. A
  `flock` on `.master_pi.lock` makes the second run exit quietly with status 0.

### Added
- **The mobile BUDGET tab can edit a category in place.** It saves BASIC with stale-write
  protection, adds or removes one-off amounts, shows the month's matching transactions,
  and keeps safe ADDITIONAL writes in the offline queue.
- **Savings goals and subaccounts with distinct IBANs can be learned from CHECK ME.**
  The import keeps a validated counterparty IBAN without changing transaction hashes;
  bank-specific phrases or an account-holder match can propose an unknown IBAN, and
  desktop/mobile can map it to an existing account or a new zero-balance asset. Later
  deposits and withdrawals become title-independent transfers in either direction.
  Full IBAN state is token-only, browser APIs show only the last four digits, false
  discoveries stay dismissed until explicitly allowed again, and mappings can be
  forgotten without rewriting earlier transactions. Only rows actually proposed by
  discovery can create a “not a goal” decision; unrelated CHECK ME rows cannot.
- **`install.sh` takes several sync times**, comma-separated, and renders one
  `OnCalendar` line per pass — for banks that post incoming transfers in settlement
  sessions rather than on arrival. The prompt suggests a pass a quarter of an hour
  after each session. Leftover attempts go to the watchdog, which now sits at the
  end of the day so its retries don't spend bank calls between passes; ask for no
  spare attempts and it isn't installed at all. The rendering logic moved out of
  the installer into `deploy/schedule.py`, with `tests/test_schedule.py` over it.

### Changed
- **The mobile stylesheet now lives in `static/mobile.css` and uses shared design
  tokens.** The service worker precaches it, all controls meet the 44px touch target,
  and the public-only goal and IBAN actions retain their teal treatment.
- **`POST /api/basic` accepts an optional `expected_amount` precondition.** Stale edits
  receive HTTP 409 with the current value instead of overwriting a newer BASIC; clients
  that omit the field keep last-write-wins behaviour.
- **Anthropic defaults moved to Claude Haiku 5.5 for categorization and Claude
  Sonnet 5.5 for learning.** Both use medium effort and a 16,000-token response
  budget. Thinking blocks are ignored while all text blocks are joined; refusals,
  truncation and textless replies are handled explicitly. Categorization refusals
  go to CHECK ME without retrying the bank fetch, learning refusals preserve the
  correction files, and Sonnet learning alone opts into server-side fallback.
  Explicitly selected older models receive none of the 5.5-only parameters.
- **Scheduling documentation now counts every bank call.** Multiple daily passes
  work against the open-day window. The separate weekly balance check also spends
  quota: with a typical four-call limit, three transaction passes plus that check
  leave no watchdog reserve on its scheduled day.
- **Card payments read merchant first.** Some Polish banks send them as `VISA PLAT
  <card> PŁATNOŚĆ KARTĄ <amount> PLN  <merchant>`, burying the one part you check a
  category against. TRANSACTIONS, CHECK ME and the mobile list now show
  `<merchant> | <card details>` (refunds, card transfers, ATM withdrawals and
  foreign-currency payments too). Display only: the database keeps the bank string,
  and the editable description switches back to it while you edit. Descriptions from
  other banks render unchanged.
- **Telegram is quiet on empty runs.** A sync pass that brings no new transactions
  and had no failures sends nothing instead of "(no new transactions)". This makes
  running the sync once per bank settlement session practical — see
  [Scheduling](docs/CONFIGURATION.md#scheduling). Failures and partial runs still
  notify, and the self-checker report still confirms the pipeline is alive.

## 1.0 — 2026-08-13 · first public release

Version numbering restarts here: 2.0 was the private app, 1.0 is the first
release anyone else can install. Existing installs upgrade in place; migrations
0006–0008 run at startup and carry the old calculator across.

### Added
- **`install.sh`** — checks prerequisites, builds the virtualenv, asks for a port,
  variant, password and sync schedule, writes `.env`, renders systemd and nginx
  templates.
- **`/setup`** — first-run screen: pick a category preset, uncheck what you don't
  want, name your accounts, choose a currency from a list, optionally configure the
  calculator. A fresh install redirects here until it's done.
- **Presets** (`presets/`) — household, Polish household, freelancer. Starter data is
  a choice now, not something baked into the schema.
- **A password** (`MB_PASSWORD_HASH`) in front of every page and endpoint, and
  `MB_API_TOKEN` so the pipeline can still reach the API. Leaving the password empty
  keeps the old open behaviour and says so at startup.
- **Login lockout** — five wrong passwords from one address and it waits 15 minutes.
  Failures are logged. Reads the proxy-written end of `X-Forwarded-For`, not the
  client-supplied start, so the bucket can't be rotated away.
- **`MB_HTTPS=1`** marks the session cookie `Secure` when something in front
  terminates TLS. Off by default, or the cookie would never arrive on a plain-http
  LAN install.
- **Security headers** on every response: CSP, `X-Frame-Options`, `nosniff`,
  `Referrer-Policy`.
- **Telegram notifications are a setup question**, not a homelab leftover: `install.sh`
  walks through BotFather, writes both values to `.env` and sends a test message.
  README documents doing it by hand. `TELEGRAM_BOT_TOKEN` replaces the old
  `TELEGRAM_TOKEN_MONEYBADGER`, which is still read so existing `.env` files work.
- **Local LLM support** — `LLM_PROVIDER=openai` with `LLM_BASE_URL` points the
  categorizer at Ollama, LM Studio or anything OpenAI-compatible.
- **Three install variants** from one codebase: basic, plus bank sync, plus automatic
  categorization.
- **`tools/demo_seed.py`** — a database of invented data for trying the app out and
  for the screenshots.
- **Docs**: CONFIGURATION.md and ENABLE_BANKING.md; README, ARCHITECTURE, API and
  DEPLOYMENT rewritten for someone who didn't build this.
- MIT licence, `requirements*.txt`, `.env.example`, and systemd units for all seven
  services (five previously existed only on one machine).

### Changed
- **The calculator is configurable.** Any number of line items with names, units and
  rates, plus two configurable tax rates and the categories to write to — replacing
  three fixed airline items with Polish tax rates hardcoded in two places. Existing
  data migrates; the numbers on screen don't move.
- **Currency and locale are settings**, injected into every page, replacing `pl-PL`
  and a literal `zł` scattered across six templates and four scripts.
- **Migrations build the schema and nothing else.** They used to seed 110 categories,
  twelve accounts and a house-renovation plan.
- **The pipeline reads the live category tree** instead of a hardcoded copy, removing
  the drift that made the model invent category names.
- **Bank wording is configuration** — transfers, cash withdrawals, card top-ups and
  bundled payments that need splitting are all described in `sync/config.json`.
- **IBAN handling no longer assumes Poland.** The country prefix comes from the
  configured bank.
- One HTTP client (`budget_client.py`) for the whole pipeline, replacing five
  hand-rolled urllib call sites, with `BUDGET_URL` from the environment.
- Navigation lives in one template include rather than being copy-pasted into seven.
- `config.json` and `rules.json` are no longer tracked; `.example` versions ship
  instead.

### Fixed
- **CSV exports quote out spreadsheet formulas.** A transaction title is chosen by
  whoever sends you the transfer, and Excel runs a cell starting with `= + - @` the
  moment the file opens. Text columns now get a leading `'`; amounts are untouched.
- `/api/export/transactions.csv` validates `year` and `month` like the rest of the
  API — a typo returned 500 instead of 400.
- `apply` on the calculator reports a missing category instead of returning success
  and writing nothing.
- The PWA offline-queue test had been passing vacuously since late July — its
  extraction marker moved when the shared helpers were split out.
- Two UI smoke checks had been dead since August 1st: they filled a date the
  transactions page wasn't showing.
- Money amounts are rounded at the API boundary, so 850.0000000000001 no longer
  reaches the browser.
- `fetch` runs without an LLM configured, categorizing with rules alone instead of
  exiting.

### Removed
- The Santander CSV import path, broken since before the first commit — it imported a
  module that never existed in the repository.
- Logo generator, a dev-server log, a spent repair script and a one-off CSS audit
  report.

## 2.0 — 2026-07-10 · "Money Badger"

### Changed
- **Renamed Home Badger → Money Badger** (UI, PWA, repo; `budget.lan` URL and
  on-Pi paths/services unchanged).
- All remaining Polish UI strings translated — the interface is now fully
  English (user data stays as typed).
- Schema management moved from ad-hoc `init_db()` to **versioned migrations**
  (`migrations.py`, `schema_migrations` table); database now runs in WAL mode
  with query-path indexes.

### Added
- **Mobile PWA** (`/m`): 3-tap expense entry, balances, budget status;
  installable (manifest + service worker), same database.
- **OVERVIEW tab**: yearly planned/spent bars + income line with tooltips,
  category × month spending matrix.
- **CSV exports**: transactions and budget plan-vs-actual.
- **Transaction delete** (UI + API) with income-received rollback.
- **Automated database backups**: daily local snapshots (30-day retention),
  weekly push to a private GitHub repo.
- Backend test suite (pytest, 25 tests) + Playwright UI smoke test.

### Fixed
- Uncategorized income transactions no longer *decrease* the computed account
  balance while awaiting review.
- Editing an income transaction's amount/date now keeps INCOME "received" in
  sync; deleting rolls it back.
- Deleting an in-use income category returns a clean error instead of a 500.
- Accounts deleted by the user no longer resurrect on service restart
  (legacy seed re-insert bug); resurrected zombies removed by migration.
- TRANSACTIONS summary bar now uses the viewed month's BASIC values (was
  always the current month's).
- Manual balance edits log the diff against the *displayed* balance, not the
  stale checkpoint.
- Spending booked directly on a parent category is counted in summaries.

## 1.2 — 2026-07-05 · CHECK ME fixes + Allegro

- CHECK ME bucketing by `tx_type` (uncategorized expenses no longer listed as
  income); `CHECK ME` literal no longer collides with the income category of
  the same name.
- Allegro purchase matching via Gmail IMAP (order e-mails → CHECK ME
  descriptions + LLM categorization).
- Computed balances: transfers push money between accounts
  (cash withdrawals, PLANET CASH); balance-bar refresh after edits.

## 1.1 — 2026-07-02/03 · Pipeline on the Pi + computed balances

- Bank fetch/categorize/push pipeline moved from the Mac to the Pi
  (`money-badger-sync.timer`, daily 10:30, watchdog retries).
- Account balances became *computed*: checkpoint + transactions since anchor;
  Enable Banking balance fetch is warn-only.
- Mixed date formats normalized to ISO; flexible date parsing on write.

## 1.0 — 2026-07-01 · "Home Badger"

- Renamed from "Budget App", full redesign (design tokens, honey badger logo).
- Per-month BASIC budgets with copy-to-next-month; income entries system;
  CHECK ME tab; English UI; roster calculator with VAT/PIT auto-budgeting.
