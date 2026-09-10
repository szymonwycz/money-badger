# Changelog

Notable changes to Money Badger. Dates are Europe/Warsaw.

## Unreleased

### Added
- **`install.sh` takes several sync times**, comma-separated, and renders one
  `OnCalendar` line per pass — for banks that post incoming transfers in settlement
  sessions rather than on arrival. The prompt suggests a pass a quarter of an hour
  after each session. Leftover attempts go to the watchdog, which now sits at the
  end of the day so its retries don't spend bank calls between passes; ask for no
  spare attempts and it isn't installed at all. The rendering logic moved out of
  the installer into `deploy/schedule.py`, with `tests/test_schedule.py` over it.

### Changed
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
