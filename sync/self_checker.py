"""The nightly self-checker — audits data that has already been fetched (SQLite,
logs, .bank_sync_state.json, raw_pulls/) without a single call to Enable
Banking. It never imports banks.enable_banking — that guarantee holds
by construction, not by convention: the daily ASPSP quota is shared with the
regular sync, and the audit must not spend a call the sync needs.

Report-only: it fixes nothing on its own. Every repair so far has needed a
human decision — the audit just says where to look.

Findings are phrased for someone reading them on a phone at breakfast, so they
name the account and the number, not the internal check that produced them.
"""
import json
import re
import sqlite3
import time
from datetime import date, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).parent
ROOT_DIR = BASE_DIR.parent

import sys
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(BASE_DIR))
import iban as _iban  # noqa: E402
from logging_setup import start_logging  # noqa: E402
from notify_telegram import send_telegram  # noqa: E402

DB_PATH = ROOT_DIR / "budget.db"
REPORT_FILE = ROOT_DIR / ".self_checker_report.txt"  # waits for the morning send, see send_self_checker_report.py
KNOWN_ISSUES_FILE = ROOT_DIR / "known_issues.json"  # {"match": "...", "note": "...", "added": "YYYY-MM-DD"}[]

STALE_FETCH_DAYS = 2
BALANCE_CHECK_LOOKBACK_DAYS = 10  # balance check is weekly now, allow a week + slack before flagging as broken
BALANCE_DIFF_TOLERANCE = 5.0
ALLEGRO_STALE_DAYS = 2
CHECKME_STALE_DAYS = 3
RAW_PULL_MIN_AGE_SECONDS = 6 * 3600

# "budget.lan" was the old wording; logs written before the rename are kept for
# 90 days, so both spellings have to parse.
# The currency is configurable and the wording was Polish until 2026-08-13, so
# both spellings have to parse: logs are kept for 90 days and a balance check
# that silently stops matching is worse than none at all.
DIFF_RE = re.compile(
    r"⚠ (?P<name>.+?): (?:app|budget\.lan)=([\d.]+)\D+ bank=([\d.]+)\D+"
    r"\((?:difference|różnica) (?P<diff>[+-][\d.]+)"
)


def sync_ran_today() -> bool:
    """Has today's pipeline run finished successfully?

    This audit is scheduled for 23:45, long after the daily sync, but it is also
    run by hand — and before the day's sync every check that looks for today's
    output is guaranteed to find nothing. Reporting that as a fault trains people
    to ignore the report.
    """
    return (ROOT_DIR / "logs" / f".success_master_pi_{date.today().isoformat()}").exists()


def check_stale_fetch(cfg: dict) -> list[str]:
    state_path = BASE_DIR / ".bank_sync_state.json"
    if not state_path.exists():
        return ["missing .bank_sync_state.json"]
    state = json.loads(state_path.read_text())
    last_fetch = state.get("last_fetch", {})
    account_names = cfg.get("account_names", {})
    today = date.today()

    # The checkpoint a successful sync writes is *yesterday* — it fetches up to
    # the last fully booked day. So one day of lag is the healthy steady state,
    # and two is normal for the hours between midnight and the daily run.
    threshold = STALE_FETCH_DAYS if sync_ran_today() else STALE_FETCH_DAYS + 1

    findings = []
    for iban in cfg.get("own_ibans", []):
        full_iban = _iban.full_iban(
            iban, (cfg.get("enable_banking", {}).get("default_aspsp") or {}).get("country", ""))
        name = account_names.get(iban, iban[-4:])
        last = last_fetch.get(full_iban)
        if not last:
            findings.append(f"{name}: no successful fetch ever")
            continue
        days = (today - date.fromisoformat(last)).days
        if days >= threshold:
            findings.append(f"{name}: last successful fetch {days} days ago ({last})")
    return findings


def _last_diff_per_account(log_path: Path) -> dict[str, float]:
    """If someone ran fetch-balances by hand more than once that day, take the
    last entry per account — the day's final word."""
    diffs = {}
    if not log_path.exists():
        return diffs
    for line in log_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        m = DIFF_RE.search(line)
        if m:
            diffs[m.group("name")] = float(m.group("diff"))
    return diffs


def check_balance_discrepancy() -> list[str]:
    """fetch-balances now runs weekly (Saturday 02:30, money-badger-balance-check.timer)
    instead of on every sync/watchdog hop — it's just a drift check against the app's
    own transaction-computed balance, never the source of truth for what's displayed, so
    daily sampling was never actually needed. One sample a week means there's no day-to-day
    trend to compare (the old "shrinking lag" heuristic doesn't apply) — just flag whichever
    account's most recent weekly diff exceeds tolerance, and flag if the check itself has
    gone quiet for longer than a week (timer broken)."""
    today = date.today()
    for i in range(BALANCE_CHECK_LOOKBACK_DAYS):
        d = today - timedelta(days=i)
        diffs = _last_diff_per_account(ROOT_DIR / "logs" / f"sync_{d.isoformat()}.log")
        if diffs:
            return [
                f"{name}: {diff:+.2f} diff from bank ({d.isoformat()} check)"
                for name, diff in diffs.items()
                if abs(diff) >= BALANCE_DIFF_TOLERANCE
            ]
    return [f"no balance check found in the last {BALANCE_CHECK_LOOKBACK_DAYS} days — money-badger-balance-check.timer broken?"]


def check_duplicate_income_balance_adjust() -> list[str]:
    if not DB_PATH.exists():
        return []
    con = sqlite3.connect(str(DB_PATH))
    try:
        rows = con.execute("""
            SELECT account, date, amount, GROUP_CONCAT(tx_type)
            FROM transactions
            WHERE tx_type IN ('Income', 'Balance Adjust')
            GROUP BY account, date, amount
            HAVING COUNT(DISTINCT tx_type) > 1
        """).fetchall()
    finally:
        con.close()
    return [f"{acc} {d} {amt:+.2f} — {types}" for acc, d, amt, types in rows]


def check_orphan_raw_pulls(cfg: dict) -> list[str]:
    raw_pulls_dir = ROOT_DIR / "raw_pulls"
    if not raw_pulls_dir.exists() or not DB_PATH.exists():
        return []
    account_names = cfg.get("account_names", {})
    con = sqlite3.connect(str(DB_PATH))
    findings = []
    cutoff = time.time() - RAW_PULL_MIN_AGE_SECONDS
    try:
        for f in sorted(raw_pulls_dir.glob("*.json")):
            if f.stat().st_mtime > cutoff:
                continue  # may still be waiting for a commit later the same day
            try:
                bare_iban, date_from, date_to, _ts = f.stem.split("_")
            except ValueError:
                continue
            name = account_names.get(bare_iban, bare_iban[-4:])
            try:
                body = json.loads(f.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            raw_txs = body.get("transactions") or []
            # The same sign conversion as EnableBankingFetcher._convert_tx,
            # duplicated on purpose: banks.enable_banking is not imported here
            # under any circumstances, so no audit path can accidentally drag in
            # code that calls the bank.
            nonzero = [
                t for t in raw_txs
                if float(((t.get("transaction_amount") or {}).get("amount", 0)) or 0) != 0
            ]
            if len(nonzero) < 2:
                continue
            count = con.execute(
                "SELECT COUNT(*) FROM transactions WHERE account=? AND date BETWEEN ? AND ?",
                (name, date_from, date_to),
            ).fetchone()[0]
            if count == 0:
                findings.append(
                    f"{name}: {len(nonzero)} tx fetched {date_from}..{date_to} "
                    f"({f.name}), missing from budget.db"
                )
    finally:
        con.close()
    return findings


def check_allegro_backlog() -> list[str]:
    today = date.today()
    findings = []
    # Skip today entirely until the sync has run — Allegro matching is its last
    # step, so before then "no log for today" is just the time of day.
    start = 0 if sync_ran_today() else 1
    for i in range(start, ALLEGRO_STALE_DAYS):
        d = today - timedelta(days=i)
        log_path = ROOT_DIR / "logs" / f"allegro_match_{d.isoformat()}.log"
        if not log_path.exists():
            findings.append(f"no allegro_match log for {d.isoformat()} (not run?)")
            continue
        text = log_path.read_text(encoding="utf-8", errors="ignore")
        # A day's log can hold multiple runs (manual debugging, retries) — only
        # the LAST run is the day's final word, same reasoning as
        # _last_diff_per_account taking the last balance line per account.
        # Otherwise one bad ad-hoc invocation early in the day would keep
        # flagging for the rest of the day even after a clean scheduled run.
        last_run = text.rsplit("START allegro_match", 1)[-1]
        if "IMAP error" in last_run:
            findings.append(f"allegro_match {d.isoformat()}: IMAP error")
        elif "no LLM configured" in last_run:
            findings.append(f"allegro_match {d.isoformat()}: no LLM configured")
    return list(dict.fromkeys(findings))


def check_checkme_backlog() -> list[str]:
    if not DB_PATH.exists():
        return []
    con = sqlite3.connect(str(DB_PATH))
    try:
        cutoff = (date.today() - timedelta(days=CHECKME_STALE_DAYS)).isoformat()
        count = con.execute("""
            SELECT COUNT(*) FROM transactions
            WHERE category_id IS NULL AND income_category_id IS NULL
              AND tx_type NOT IN ('Balance Adjust', 'Money Transfer')
              AND ignored = 0 AND date < ?
        """, (cutoff,)).fetchone()[0]
    finally:
        con.close()
    return [f"{count} CHECK ME items older than {CHECKME_STALE_DAYS} days"] if count else []


def _load_known_issue_matchers() -> list[str]:
    """known_issues.json — flat list of {"match": "...", "note": "...", "added": "..."}.
    Any finding whose text contains a "match" substring is triaged/deferred and
    gets suppressed from the nightly report until the entry is removed. Edit
    this file directly (SSH) when you've acknowledged something and don't want
    it repeating every night — remove the entry once it's actually fixed."""
    if not KNOWN_ISSUES_FILE.exists():
        return []
    try:
        issues = json.loads(KNOWN_ISSUES_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    return [i["match"] for i in issues if i.get("match")]


def main():
    log_path = start_logging("self_checker", ROOT_DIR)
    print(f"(log: {log_path})")
    cfg = json.loads((BASE_DIR / "config.json").read_text())
    known_matchers = _load_known_issue_matchers()

    sections = {
        "Stale accounts (no fetch)": check_stale_fetch(cfg),
        "Balance discrepancy (weekly check)": check_balance_discrepancy(),
        "Duplicate Income/Balance Adjust": check_duplicate_income_balance_adjust(),
        "raw_pulls not committed to budget.db": check_orphan_raw_pulls(cfg),
        "Allegro-match": check_allegro_backlog(),
        "Stale CHECK ME backlog": check_checkme_backlog(),
    }

    lines = []
    suppressed = 0
    for title, findings in sections.items():
        for f in findings:
            line = f"⚠ {title}: {f}"
            print(f"  {line}")
            if any(m in line for m in known_matchers):
                suppressed += 1
                continue
            lines.append(line)

    if suppressed:
        print(f"  ({suppressed} finding(s) suppressed — known_issues.json)")

    if lines:
        msg = f"🔍 Money Badger — nightly audit ({date.today().isoformat()})\n\n" + "\n".join(lines)
        # Not sent from here: send_self_checker_report.py reads this file on its
        # own morning timer, so a nightly audit never buzzes anyone awake.
        REPORT_FILE.write_text(msg, encoding="utf-8")
        print(f"\n{len(lines)} finding(s) — report saved, sending at 08:30.")
    else:
        if REPORT_FILE.exists():
            REPORT_FILE.unlink()  # yesterday's problem resolved itself — don't send a stale report
        if suppressed:
            print("\nAll clear except known/suppressed issues — no report.")
        else:
            print("\nAll clear — no report.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # Silence from a self-checker that crashed looks exactly like a clean
        # night, so this is the one case that always notifies, immediately.
        send_telegram(f"❌ Money Badger self_checker crashed: {e!r}")
        raise
