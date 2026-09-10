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
| `LLM_PROVIDER` | one of the names below | Defaults to `anthropic`. |
| *its key* | one variable per provider, see the table | |
| `LLM_MODEL` | | The model id. Written by `install.sh` from the provider's own list. |
| `LLM_BASE_URL` | the provider's URL | Overrides it — a local server is just this API somewhere else. |
| `LLM_API_KEY` | the provider's key | Overrides it. Usually unnecessary for a local server, which wants no key at all. |

| `LLM_PROVIDER` | Key | Endpoint |
|---|---|---|
| `anthropic` | `ANTHROPIC_API_KEY` | `https://api.anthropic.com/v1` |
| `openai` | `OPENAI_API_KEY` | `https://api.openai.com/v1` |
| `deepseek` | `DEEPSEEK_API_KEY` | `https://api.deepseek.com/v1` |
| `moonshot` | `MOONSHOT_API_KEY` | `https://api.moonshot.ai/v1` (Kimi) |
| `gemini` | `GEMINI_API_KEY` | `https://generativelanguage.googleapis.com/v1beta/openai` |
| `xai` | `XAI_API_KEY` | `https://api.x.ai/v1` (Grok) |

Everything except Anthropic speaks the OpenAI chat-completions shape, and so does
Ollama, LM Studio and vLLM — for those, set `LLM_PROVIDER=openai` and point
`LLM_BASE_URL` at them, e.g. `http://127.0.0.1:11434/v1`. Any other service with
a compatible endpoint works the same way, without waiting for it to be listed
here.

### Models

No model ids are baked into the app. `install.sh` asks the provider what it
offers after you paste the key, and writes your choice to `LLM_MODEL`. To see the
same list later:

```bash
venv/bin/python sync/llm.py --models
```

If the model in `LLM_MODEL` is retired — providers do that on a schedule — the
next run does not fail: it asks for the list again, takes the nearest model of
the same family, and logs the swap:

```
  [llm] model deepseek-chat is gone at deepseek — using deepseek-chat-v4 instead.
        Set LLM_MODEL=deepseek-chat-v4 in .env to make it permanent.
```

If nothing in that family is on offer, or the list cannot be fetched, the run
stops with the ids that *are* available rather than quietly categorizing nothing.

On Anthropic, leaving `LLM_MODEL` empty keeps the original behaviour: a cheap
model for categorizing (it runs on every unmatched transaction) and a stronger
one for learning rules (once a day, over a handful of examples). Setting
`LLM_MODEL` collapses that to one model for both.

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

`install.sh` asks when to sync — one time, or several separated by commas — plus
how many attempts a day in total, and renders the systemd timers from that. To
change it later, edit the `OnCalendar` lines in
`/etc/systemd/system/money-badger-sync*.timer` and run `sudo systemctl daemon-reload`.

`Persistent=true` is set on both timers, so systemd replays a trigger it thinks
was missed. Restarting a timer after a time that has already passed today
therefore runs a pass immediately — expected, but it does spend a bank API call.

Retries are free when the day's sync already succeeded — the run is skipped — but
each genuine retry spends one bank API call, and most banks allow only a handful
per day.

### More than one fetch a day

Most banks don't post an incoming transfer the moment it arrives — they post it in
settlement sessions, a few times per working day. A single morning sync therefore
shows today's incoming money tomorrow. Find your bank's session times (they are
usually published, and they differ per bank and per country), and put a pass about
a quarter of an hour after each one:

| Bank posts at | Answer to give install.sh |
|---|---|
| 11:00, 15:00, 17:30 | `11:15,15:15,17:45` |
| once, overnight | `10:30` |

The installer renders one `OnCalendar` line per time and spreads whatever attempts
are left over onto the watchdog. Ask for enough attempts to cover the passes and
one retry — with a typical quota of four calls per account per day, three passes
plus one retry is the ceiling. Beyond it the bank answers 429 and skips that
account until midnight.

The watchdog is deliberately pushed to the end of the day when there are several
passes: an earlier retry would fall between your own passes and spend a call right
before one of them. If every attempt goes to a pass, the watchdog timer isn't
installed at all.

Telegram only speaks up when a pass brings new transactions or an account fails, so
the extra passes stay quiet on a slow day. The daily self-checker report is what
tells you the sync is still alive.

---

## Order matching from email

A card payment says `AMAZON MKTPL` and an amount; the order confirmation in your
inbox says what it bought. `sync/order_match.py` reads those emails and fills in
the description and category of the matching CHECK ME transaction.

Two things have to be set up: the mailbox, and the shops.

### The mailbox

In `.env`:

| Key | Default | |
|---|---|---|
| `MAIL_ADDRESS` | | The mailbox the confirmations arrive at. |
| `MAIL_PASSWORD` | | Its password. Wherever the provider offers **app passwords**, use one — Gmail requires it, and it can be revoked without changing your account password. |
| `MAIL_IMAP_HOST` | `imap.gmail.com` | Any IMAP server: `imap.fastmail.com`, `imap.mail.me.com`, your own Dovecot. |
| `MAIL_IMAP_PORT` | `993` | Implicit TLS only, which is what 993 means everywhere. |

`GMAIL_ADDRESS` and `GMAIL_APP_PASSWORD` are the names the first version used and
are still read, so an `.env` written before this needs no editing. Without an
address and a password the step skips itself silently, like the other optional
ones.

Mail is read from one **folder per shop** — never from the inbox, and never by
guessing at senders or subject lines. Write one server-side rule per shop
(`from:(@amazon.de) → Amazon`); on Gmail a label *is* a folder, so a filter that
applies the label is the whole setup. Sorting on the server means anything that
lands in the folder is read, including mail forwarded from a second account. The
folder is opened directly rather than INBOX, because a Gmail filter that labels
usually archives as well.

### The shops

```json
"order_matching": {
  "local_currency": "PLN",
  "fx_rates": { "EUR": 4.30, "USD": 4.00 },
  "fx_tolerance_pct": 5,
  "shops": {
    "Allegro":    { "folder": "Allegro" },
    "Amazon":     { "folder": "Amazon" },
    "eBay":       { "folder": "eBay" },
    "Temu":       { "folder": "Temu" },
    "AliExpress": { "folder": "AliExpress" }
  }
}
```

In `sync/config.json`. Leave the whole block out and only Allegro is read, from
the `Allegro` folder — which is what installs made before this existed already do.

- `shops` — the shops to read, each with the folder its confirmations are filed
  into. The key is the name written into the transaction description. A folder
  that does not exist is reported and skipped, not an error.
- `local_currency` — the currency your accounts are in, as an ISO code. What the
  statement is denominated in.
- `fx_rates` — approximate rate per foreign currency, in units of
  `local_currency`. Only needed for shops that bill you in another one.
- `fx_tolerance_pct` — how far a converted amount may miss, in percent.

Allegro is parsed from its own template: exact, instant and free. Every other
shop is read by the LLM — the whole email goes to the model and comes back as an
order. That is slower and it costs a fraction of a cent per email, but it works
without anyone having seen that shop's template first, and it survives the shop
redesigning it. An email that has already been matched is never sent again.

### Foreign currencies

An order in EUR reaches the statement in your account currency, converted at the
bank's own rate plus its spread, so an exact match is impossible. The rate from
`fx_rates` converts the order and a transaction counts as the same purchase when
it lands within `fx_tolerance_pct` of that, and within five days of the order
date.

**Without a rate for that currency nothing is guessed.** The order is reported in
the log and left for you — an approximate rate you set yourself is the only thing
that makes a foreign match possible, and a stale one only narrows what matches,
never widens it into the wrong transaction.

### When it declines to act

Anything ambiguous is left in CHECK ME rather than resolved by guesswork: two
transactions in the amount and date window, no transaction at all, a model reply
that is missing a field or invented one, an unknown currency. Matching a payment
to the wrong order is worse than not matching it, and an unmatched order is
retried on the next run — its transaction may only appear on the statement a day
or two later.

Processed orders are remembered in `sync/.allegro_processed.json`, and the run
logs to `logs/allegro_match_<date>.log`; both keep the old name so existing state
and the nightly self-check carry over. Delete the state file to re-match
everything from the last 30 days.
