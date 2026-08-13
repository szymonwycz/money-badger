# Configuration

Three places, by lifetime:

| Where | Holds | Changed by |
|---|---|---|
| `.env` | Secrets and anything needed before the database opens | You, in an editor |
| `settings` table | Currency, locale, calculator wiring | The app, at `/setup` and on the calculator tab |
| `sync/config.json` | Bank accounts and how to read your bank's wording | You, in an editor |

`install.sh` writes a working `.env`; `.env.example` documents every key.

---

## `.env`

Loaded two ways: systemd reads it through `EnvironmentFile`, and the app reads
it itself for anyone starting gunicorn by hand. Real environment variables win
over the file, so systemd or your shell can override it.

### Web app

| Key | Default | |
|---|---|---|
| `MB_PASSWORD_HASH` | empty | The login password, hashed. **Empty disables the login entirely** — every endpoint, including the ledger export and delete, becomes reachable by anyone who can connect. |
| `SECRET_KEY` | random per process | Signs the session cookie. Leaving it empty logs everyone out on every restart, and on every gunicorn worker. |
| `MB_API_TOKEN` | empty | Lets the sync pipeline call the API without a browser session. Only needed once a password is set. |
| `MB_HTTPS` | unset | Set to `1` when something in front of the app terminates TLS. Marks the session cookie `Secure`, so it is never sent over plain HTTP. Leave unset on a plain-http LAN install or the cookie will not arrive at all. |
| `BUDGET_DB` | `budget.db` beside `app.py` | Database path. |

Generating a password hash:

```bash
venv/bin/python -c "from werkzeug.security import generate_password_hash as h; \
                    print(h(input('password: ')))"
```

`SECRET_KEY` and `MB_API_TOKEN` are just random strings:

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

### Pipeline

| Key | Default | |
|---|---|---|
| `BUDGET_URL` | `http://127.0.0.1:5055` | Where the pipeline reaches the app. Change it only if they run on different hosts. |

### Categorization

Without any of these the pipeline still runs: keyword rules do what they can and
the rest lands in CHECK ME.

| Key | | |
|---|---|---|
| `LLM_PROVIDER` | `anthropic` \| `openai` | `openai` means *any* OpenAI-compatible server, including local ones. |
| `ANTHROPIC_API_KEY` | | For `anthropic`. |
| `LLM_BASE_URL` | | For `openai`, e.g. `http://127.0.0.1:11434/v1` for Ollama. |
| `LLM_MODEL` | provider default | Overrides both the categorization and rule-learning model. |
| `LLM_API_KEY` | | Usually unnecessary for a local server. |

Two models are used by default: a cheap one for categorizing (it runs on every
unmatched transaction) and a stronger one for learning rules (once a day, over a
handful of examples). Setting `LLM_MODEL` collapses that to one.

### Optional integrations

| Key | |
|---|---|
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | A message after each sync, and the nightly audit report in the morning. |
| `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD` | Receipt matching from a Gmail label. Needs an app password, not your account password. |

---

## The `settings` table

Written by `/setup` and by the calculator tab; readable and writable at
`/api/settings`. Editing by hand is fine:

```bash
sqlite3 budget.db "UPDATE settings SET value='€' WHERE key='currency'"
```

| Key | Example | |
|---|---|---|
| `currency` | `€` | Symbol appended to every amount. |
| `locale` | `en-IE` | Number format. `de-DE` gives 1.234,56; `en-IE` gives 1,234.56. |
| `calc.enabled` | `1` | Whether the calculator tab exists. |
| `calc.tab_label` | `CALCULATOR` | What it's called in the navigation. |
| `calc.vat_rate` | `0.23` | **Empty means not applicable**, which is different from `0` — the row disappears instead of showing zero. |
| `calc.vat_label` | `VAT` | |
| `calc.tax_rate` | `0.085` | Same rules as VAT. |
| `calc.tax_label` | `PIT` | |
| `calc.income_category` | `Contract work` | Income category the gross figure is written to. Must exist. |
| `calc.vat_category` | `Taxes: VAT` | Expense category for the VAT set-aside, as `Parent: Child`. |
| `calc.tax_category` | `Taxes: Income tax` | Same, for the income tax. |
| `calc.month_offset` | `1` | For pay in arrears: 1 means the work counted in this month was done last month. |
| `calc.apply_note` | `Calculator` | Written on every row the calculator creates, and how it finds them again to replace. |

"Apply to budget" fails loudly if a category named here doesn't exist.

---

## `sync/config.json`

Only for the bank-sync variants. Copy `sync/config.example.json` and edit.
Relative paths resolve against the project root, so the file survives being
copied to another machine.

### Accounts

```json
"account_names": { "00000000000000000000000001": "Main account" },
"own_ibans":     [ "00000000000000000000000001" ],
"savings_ibans": [ ]
```

IBANs without the country prefix and without spaces. The names must match your
account names in the app exactly, or imported transactions land on the wrong
account. Money arriving from a `savings_ibans` account is treated as drawing on
your own savings, not as new income.

### Filters

Banks describe the same event in their own words, so the phrases that classify a
row are configuration. All matching is lowercase substring matching against the
transaction title plus the counterparty name.

```json
"filters": {
  "own_transfer_titles": ["between your own accounts"],
  "transfer_to_account": [
    {"match": ["atm withdrawal"], "account": "Cash"},
    {"match": ["card payment", "revolut"], "account": "Revolut"}
  ],
  "savings_withdrawal_titles": [],
  "skip_incoming_titles": ["top-up"]
}
```

- `own_transfer_titles` — moving money between accounts you own, so it isn't spending.
- `transfer_to_account` — a rule fires when **every** phrase in `match` appears. A cash
  withdrawal is really a transfer into your cash account, and "card payment" plus
  "revolut" together mean a top-up while neither does alone.
- `savings_withdrawal_titles` — drawing on savings rather than earning.
- `skip_incoming_titles` — card top-ups book both legs, days apart and without an IBAN.
  Keep only the outgoing one or the money is counted twice.

### Split rules

One transfer that actually pays several things:

```json
"split_rules": [{
  "counterpart_iban": "00000000000000000000000009",
  "total_range": [1300.00, 1600.00],
  "parts": [
    {"amount": 200.00,     "category": "Housing: Insurance",      "label": "insurance"},
    {"amount": 500.00,     "category": "Savings",                 "label": "savings"},
    {"amount": "remainder","category": "Housing: Rent / Mortgage","label": "rent"}
  ]
}]
```

Exactly one part takes `"remainder"` and absorbs whatever the fixed parts leave.
`total_range` is the sanity check that keeps an unrelated payment to the same
account from being carved up.

### Prompt hints

Local knowledge the category names alone don't convey:

```json
"prompt_hints": [
  "EV charging stations belong under car fuel, not utilities",
  "Anything from the corner pharmacy is Health: Pharmacy"
]
```

### Enable Banking

```json
"enable_banking": {
  "app_id": "",
  "private_key_path": "sync/enable_banking.pem",
  "state_file": "sync/.bank_sync_state.json",
  "days_back": 30,
  "default_aspsp": {"name": "", "country": ""},
  "aspsp_names": {}
}
```

`default_aspsp` is the bank for every IBAN not listed in `aspsp_names` — the
common case of all your accounts being at one bank. See
[ENABLE_BANKING.md](ENABLE_BANKING.md).

---

## `sync/rules.json`

Keyword rules, checked before any LLM. Copy `sync/rules.example.json`.

```json
{
  "keywords":     { "netflix": "Subscriptions: Streaming" },
  "merchant_map": { "shell": "Transport: Fuel" },
  "patterns":     [ {"pattern": "insurance", "category": "Housing: Insurance", "priority": 1} ]
}
```

Order: `patterns` (highest priority first), then `merchant_map`, then `keywords`.
First hit wins. Category names must exist in your app or the rule is dropped.

You do not have to write these. Every correction you make in CHECK ME is fed back
through `main.py learn`, which writes new rules here. Starting from the near-empty
example and letting it learn for a couple of months is the intended path.

Note that a keyword is a plain substring: `"bp "` matches `"exchanged to gbp "`.
Rules are matched against the title and counterparty joined and stripped, so a
trailing space no longer saves you.

---

## Scheduling

`install.sh` asks how often to sync and renders the systemd timers. To change it
later, edit the `OnCalendar` lines in `/etc/systemd/system/money-badger-sync*.timer`
and run `sudo systemctl daemon-reload`.

Retries are free when the day's sync already succeeded — the run is skipped — but
each genuine retry spends one bank API call, and most banks allow only a handful
per day.
