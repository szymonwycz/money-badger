# Deployment and operations

`./install.sh` covers the first-time setup — see the [README](../README.md).
This is what comes after: what runs, how to update it, and how to get your data
back when something goes wrong.

## What ends up running

| | |
|---|---|
| App | gunicorn on `127.0.0.1:PORT`, **one worker** |
| Database | `budget.db` beside `app.py` (SQLite, WAL) |
| Config | `.env` (mode 600), `sync/config.json`, `sync/rules.json` |
| Services | `money-badger.service`, plus sync/backup timers if you installed them |
| Logs | `logs/` for the pipeline, `journalctl -u money-badger` for the app |

One worker is deliberate. SQLite takes a single writer, and two gunicorn workers
racing on the same file produce `database is locked` under exactly the conditions
you'd least want it — a bulk import while someone edits on their phone. Raising it
means moving to Postgres first.

## Exposing it

The app binds to localhost. How it becomes reachable is up to you:

- **VPN only** (Tailscale, WireGuard) — simplest and safest. Nothing is exposed to
  the internet; devices on the VPN reach the port directly.
- **Reverse proxy on your LAN** — `deploy/rendered/nginx.conf` is a starting point.
  It includes a commented-out IP allowlist.
- **Public, with a password** — set `MB_PASSWORD_HASH`, terminate TLS at the proxy,
  and understand you are putting your complete financial history behind one
  password. A VPN is a better answer if you have the option.

Do not expose it with the login disabled. The startup log says so for a reason.

## Updating

```bash
cd /path/to/money-badger
git pull
venv/bin/pip install -r requirements.txt      # only if requirements changed
sudo systemctl restart money-badger
curl -s -o /dev/null -w '%{http_code}\n' localhost:PORT/     # expect 200 or 302
```

Migrations run at startup, in order, each recorded in `schema_migrations`. Nothing
to run by hand.

Before anything risky, take a snapshot — `deploy/backup-db.sh` is safe against a
live database, unlike copying the file.

Templates and static files are read from disk, so a restart is enough; there is no
build step. Static assets carry a cache-busting version derived from their
modification time, so browsers won't run yesterday's JavaScript against today's
HTML.

## Backups

`deploy/backup-db.sh`, on a daily timer:

- `sqlite3 .backup` to `~/backups/money-badger/daily/budget-YYYY-MM-DD.db.gz`,
  gzipped, **90-day retention**. `.backup` is consistent against a live WAL
  database; `cp` is not.
- On Sundays, if `~/backups/money-badger/github-mirror` is a git repo, the snapshot
  is committed and pushed there. Point it at a **private** repository — this is your
  complete financial history.

Override the paths with `BACKUP_DIR` and `BACKUP_MIRROR`.

The script refuses to run when the database is missing or empty, because
`sqlite3 .backup` would happily create an empty one and the retention sweep would
then delete every real snapshot while systemd reported success.

Back up `.env`, `sync/config.json` and `sync/.bank_sync_state.json` too. Losing the
last one means re-authorizing every bank account.

### Restore

```bash
sudo systemctl stop money-badger money-badger-sync.timer
cd /path/to/money-badger
gunzip -k ~/backups/money-badger/daily/budget-2026-08-13.db.gz
mv budget.db budget.db.broken
mv ~/backups/money-badger/daily/budget-2026-08-13.db budget.db
sudo systemctl start money-badger money-badger-sync.timer
```

Keep the broken file until you've confirmed the restore — balances on the BALANCE
bar are the quickest check.

## Monitoring

```bash
systemctl status money-badger
systemctl list-timers 'money-badger*'
journalctl -u money-badger -n 50
tail -f logs/master_pi_*.log
```

If you configured Telegram, the pipeline messages you after each run, and the
nightly self-check reports in the morning. The self-check reads only data already
on disk — it never calls the bank, precisely so that an audit can't exhaust the
quota the sync needs.

Suppress a finding you've decided to live with by adding it to `known_issues.json`:

```json
[{"match": "Revolut: last successful fetch", "note": "no EB support, checked manually", "added": "2026-08-13"}]
```

## Health checks

```bash
curl -s -o /dev/null -w '%{http_code}\n' localhost:PORT/
sqlite3 budget.db 'PRAGMA integrity_check'
sqlite3 budget.db 'SELECT name, applied_at FROM schema_migrations ORDER BY name'
venv/bin/pytest tests/ sync/ -q
```

The test suite is safe to run against a production install — it builds its own
throwaway database and never opens `budget.db`.

## When the app won't start

Check the log first: `journalctl -u money-badger -n 50`.

- **`no such table`** — a migration failed halfway. The transaction should have rolled
  it back; restore from the last snapshot and open an issue with the traceback.
- **Everyone gets logged out on every restart** — `SECRET_KEY` is empty, so a new one
  is generated per process.
- **The pipeline gets 401s** — the app has a password and `MB_API_TOKEN` doesn't match
  the one in `.env`, or the pipeline isn't reading `.env` at all.
- **`database is locked`** — more than one gunicorn worker. See above.
