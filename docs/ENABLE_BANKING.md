# Connecting your bank

Money Badger pulls transactions through [Enable Banking](https://enablebanking.com),
which is a broker in front of the PSD2 APIs European banks are required to
provide. You register your own application with them, so the connection is
between you and your bank — no third party holds your data, and nothing about
this runs on someone else's server.

This is the fiddliest part of the setup. Budget half an hour, once. Everything
here is optional: the app works fine with transactions typed in by hand.

**Not in Europe?** PSD2 is an EU thing and Enable Banking covers EEA banks. The
pipeline is one module (`sync/banks/enable_banking.py`) behind a small
interface, so another provider is a contained piece of work — but there isn't
one in the box.

---

## 1. Register an application

1. Sign up at [enablebanking.com](https://enablebanking.com). The free tier is
   generous enough for a household — it is metered per API call, and this app
   makes a handful a day.
2. Create an application in the control panel. Choose **production** access, not
   sandbox, unless you want to try it against their test bank first.
3. Set the redirect URL to `https://localhost/`. Nothing listens there; you will
   copy the address out of the browser bar by hand. That is deliberate — it
   avoids running a web server just to catch one redirect.
4. Download the private key. You get a `.pem` file exactly once.

Put the key where the config expects it and lock it down:

```bash
mv ~/Downloads/your-key.pem sync/enable_banking.pem
chmod 600 sync/enable_banking.pem
```

It is gitignored. It is also the whole credential — anyone with that file and
your application id can read your accounts.

---

## 2. Fill in the config

Copy the example if `install.sh` hasn't already:

```bash
cp sync/config.example.json sync/config.json
```

Then edit it:

```json
{
  "account_names": {
    "00000000000000000000000001": "Main account",
    "00000000000000000002":       "Partner account"
  },
  "own_ibans": [
    "00000000000000000000000001",
    "00000000000000000002"
  ],
  "enable_banking": {
    "app_id": "the id from the control panel",
    "private_key_path": "sync/enable_banking.pem",
    "default_aspsp": {"name": "Your Bank", "country": "PL"},
    "aspsp_names": {
      "00000000000000000002": {"name": "Another Bank", "country": "DE"}
    }
  }
}
```

Three things to get right:

- **IBANs without the country prefix and without spaces.** `default_aspsp.country`
  supplies the prefix. If your accounts are at banks in different countries, give
  those IBANs their own `aspsp_names` entry.
- **`account_names` must match the account names in the app exactly.** They are how
  an imported transaction finds its account. A typo sends a month of spending to
  the wrong balance.
- **`name` in `aspsp_names` / `default_aspsp` must be the bank's name as Enable
  Banking spells it.** List them with:

  ```bash
  venv/bin/python -c "
  import sys; sys.path.insert(0, 'sync')
  from banks.enable_banking import EnableBankingFetcher
  import json; cfg = json.load(open('sync/config.json'))['enable_banking']
  f = EnableBankingFetcher(cfg['app_id'], cfg['private_key_path'], '/tmp/eb-state.json')
  import requests
  r = requests.get('https://api.enablebanking.com/aspsps', headers=f._headers(), timeout=30)
  print('\n'.join(sorted(a['name'] + '  (' + a['country'] + ')' for a in r.json()['aspsps'])))
  "
  ```

---

## 3. Authorize each account

```bash
cd sync
../venv/bin/python main.py fetch-setup
```

For every IBAN it will:

1. print a link — open it and log into your bank as you normally would;
2. redirect you to `https://localhost/` afterwards, which shows a connection
   error. **That is expected.** The address bar is what matters;
3. wait for you to paste that full address back into the terminal.

The consent it stores is valid for **180 days**, which is the PSD2 maximum. After
that you run `fetch-setup` again. There is no way around it — the limit is in the
regulation, not in this app.

The result goes into `sync/.bank_sync_state.json`: a session id and account id per
IBAN. Gitignored, and worth including in your backups.

---

## 4. Try it

```bash
cd sync
../venv/bin/python main.py fetch --dry-run
```

That reads the last 30 days, categorizes, and prints what it would write without
saving anything.

**Watch the quota.** Most banks allow only a few API calls per account per day
through PSD2, shared across everything you run. A handful of `--dry-run` calls
while you are debugging will exhaust the day. The app tracks this: once a bank
answers 429, that account is marked dead for the day and skipped instead of being
hammered.

Then let the timer take over:

```bash
sudo systemctl enable --now money-badger-sync.timer
```

---

## When it stops working

**"no session — run: python main.py fetch-setup"** — the consent expired or
was revoked. Re-run `fetch-setup` for that account.

**"ASPSP daily limit reached"** — the quota. It resets at midnight in the bank's
timezone. If it happens every day, reduce the attempts per day (see
[CONFIGURATION.md](CONFIGURATION.md#scheduling)).

**Transactions land on the wrong account** — `account_names` doesn't match your
account names in the app. Fix the config; existing transactions need moving by
hand on the TRANSACTIONS tab.

**A transfer between your own accounts shows up as spending** — add your bank's
wording to `filters.own_transfer_titles`. Every bank words it differently, which
is why it is configuration and not code.

**Everything lands in CHECK ME** — expected at first. The rules start nearly
empty; each correction you make teaches one. It gets quiet after a month or two.

If you want to see exactly what the bank returned, the raw responses are kept in
`raw_pulls/` for 90 days.
