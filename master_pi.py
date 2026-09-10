#!/usr/bin/env python3
"""
master_pi.py – the daily fetch/push orchestrator.

Steps: 0 fetch from the bank -> 0b learn rules from corrections -> 3 push to the
app -> 4 match orders from email.

Account balances are deliberately not part of this pipeline. The app derives a
balance from its transactions rather than from the bank, so comparing the two is
only a drift check — it runs on its own weekly timer
(money-badger-balance-check.timer) instead of spending an ASPSP call on every
sync and watchdog hop, since most banks allow only a few per day.

    python3 master_pi.py [--dry-run]

Run by money-badger-sync.timer — once a day as installed, or more often if you
add OnCalendar lines (see docs/CONFIGURATION.md#scheduling).
"""

import argparse
import fcntl
import json
import subprocess
import sys
import time
from pathlib import Path

BASE_DIR      = Path(__file__).parent
SYNC_SCRIPT   = BASE_DIR / "sync" / "main.py"
SYNC_CFG      = BASE_DIR / "sync" / "config.json"
PUSH_SCRIPT   = BASE_DIR / "push_actuals.py"
ALLEGRO_SCRIPT = BASE_DIR / "sync" / "order_match.py"

import budget_client
import env_file

env_file.load()

from logging_setup import start_logging, mark_success
from notify_telegram import send_telegram

SUMMARY_FILE = BASE_DIR / ".last_fetch_summary.json"
LOCK_FILE    = BASE_DIR / ".master_pi.lock"


def _get_checkme_count() -> str:
    try:
        return str(budget_client.get("/api/transactions/unreviewed/count", timeout=5).get("count", "?"))
    except Exception:
        return "?"


# Skip reasons that fetch()/get_balances() report themselves (see
# sync/banks/enable_banking.py). None of them heal by waiting 60 seconds: the
# ASPSP limit resets tomorrow, and an expired or missing session needs a manual
# `fetch-setup`. Retrying is only worth it for genuinely transient failures —
# the LLM API, or the app not answering during categorization — and those reach
# failed_reasons as exception text, which never matches these, so comparing the
# string is enough to tell them apart.
NON_RETRYABLE_REASONS = {
    "ASPSP daily limit reached",
    "session expired (401)",
    "no EB session (run fetch-setup)",
    "no account_id (run fetch-setup)",
}


def _only_non_retryable_failures() -> bool:
    try:
        summary = json.loads(SUMMARY_FILE.read_text())
    except Exception:
        return False
    reasons = summary.get("failed_reasons", {})
    return bool(reasons) and all(r in NON_RETRYABLE_REASONS for r in reasons.values())


def acquire_lock():
    """The lock handle, or None when a run is already in progress.

    Both money-badger-sync.timer and money-badger-sync-watchdog.timer carry
    Persistent=true. When the machine is off at the scheduled time, systemd
    replays both missed triggers in the same second — the watchdog then looks
    for the success marker the running sync hasn't written yet (it is written
    at the very end), decides the day failed and starts a second pipeline
    alongside the first. That costs two bank fetches out of one daily quota,
    two pushes, and two order-matching runs writing over each other's state.
    The lock lives on the process, so it guards a manual run started during an
    automatic one just as well.
    """
    fh = open(LOCK_FILE, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        return None
    return fh


def _should_notify(summary: dict, today_str: str) -> bool:
    """Whether this run has anything worth a Telegram message.

    The sync can be scheduled several times a day (one pass per incoming
    settlement session at the bank), and most passes find nothing. Without this
    gate every empty pass sends "(no new transactions)". Liveness is the
    self-checker's job, not this message's.

    The date is checked because run_fetch() returns early when Enable Banking
    isn't configured, leaving .last_fetch_summary.json from a previous day —
    without the check that stale list would be sent again.
    """
    if summary.get("failed_accounts"):
        return True
    if summary.get("date") != today_str:
        return False
    return any(acc.get("transactions") for acc in summary.get("accounts", []))


def run_fetch(dry_run: bool = False) -> bool:
    """Step 0: pull transactions through Enable Banking.

    Returns True and does nothing when Enable Banking is not configured — that is
    a valid setup, not a failure. Returns False when the fetch or categorization
    actually failed, even for a single account, so main() withholds the success
    marker and the watchdog retries later the same day instead of quietly writing
    the day off.
    """
    try:
        cfg = json.loads(SYNC_CFG.read_text(encoding="utf-8"))
    except Exception:
        return True
    if "enable_banking" not in cfg:
        return True

    cmd = [sys.executable, str(SYNC_SCRIPT), "fetch"]
    if dry_run:
        cmd.append("--dry-run")

    result = subprocess.run(cmd, cwd=str(SYNC_SCRIPT.parent))
    if result.returncode != 0:
        if _only_non_retryable_failures():
            print("\nEnable Banking fetch failed — ASPSP limit or session only, nothing"
                  " transient, so a retry would spend another call for nothing."
                  " Leaving it to the next watchdog hop.")
            return False
        print("\nWARNING: Enable Banking fetch failed — retrying in 60s...")
        time.sleep(60)
        result = subprocess.run(cmd, cwd=str(SYNC_SCRIPT.parent))
        if result.returncode != 0:
            print("\n❌ Enable Banking fetch failed (2/2) — some or all accounts were"
                  " not processed. Their checkpoints were not advanced, so nothing is"
                  " lost; the watchdog or tomorrow's sync will fetch them again.")
            return False
    return True


def run_learn_from_budget(dry_run: bool = False):
    """Step 0b: pull corrections from the app → Corrections/ → main.py learn."""
    if not PUSH_SCRIPT.exists():
        return
    try:
        cfg            = json.loads(SYNC_CFG.read_text(encoding="utf-8"))
        raw_dir = cfg.get("corrections_dir", "Corrections")
        corrections_dir = Path(raw_dir).expanduser()
        if not corrections_dir.is_absolute():
            corrections_dir = BASE_DIR / corrections_dir

        result = subprocess.run(
            [sys.executable, str(PUSH_SCRIPT), str(corrections_dir)],
            cwd=str(BASE_DIR),
        )
        if result.returncode != 0:
            print("  Warning: push_actuals (pulling corrections) failed.")
            return

        new_csvs = [f for f in corrections_dir.glob("budget_corrections_*.csv")]
        if not new_csvs:
            return

        print("  Learning from corrections (main.py learn)...")
        cmd = [sys.executable, str(SYNC_SCRIPT), "learn"]
        if dry_run:
            cmd.append("--dry-run")
        learn = subprocess.run(cmd, cwd=str(SYNC_SCRIPT.parent))
        if learn.returncode != 0:
            # Return code used to be ignored: learn could crash and the corrections
            # were flagged synced anyway, so the rules never learned them and nothing
            # ever retried — the flag is one-way.
            print("  Warning: main.py learn failed — the corrections stay unmarked "
                  "and will be picked up again on the next run.")
            return

        if not dry_run:
            # Mark only the correction ids that actually made it into these CSVs.
            ids = []
            for csv_path in new_csvs:
                ids_file = csv_path.with_suffix(".ids")
                if ids_file.exists():
                    ids += [int(i) for i in ids_file.read_text().split(",") if i.strip()]
            if not ids:
                print("  No correction ids returned — not marking them, so they will be retried.")
                return

            try:
                budget_client.post("/api/corrections/mark-synced", {"ids": ids}, timeout=5)
            except Exception:
                pass  # app briefly unreachable; the corrections come round again next run

    except Exception as e:
        print(f"  Warning: could not talk to the app: {e}")


def run_push_actuals():
    """Step 3: push transactions to the app (usually the same host)."""
    if not PUSH_SCRIPT.exists():
        return
    subprocess.run([sys.executable, str(PUSH_SCRIPT)], cwd=str(BASE_DIR))


def run_allegro_match():
    """Step 4: match order confirmation emails against CHECK ME transactions.

    Does nothing when no mailbox is configured (see sync/order_match.py), like
    every other optional step — it never blocks the rest of the pipeline.
    """
    if not ALLEGRO_SCRIPT.exists():
        return
    subprocess.run([sys.executable, str(ALLEGRO_SCRIPT)], cwd=str(SYNC_SCRIPT.parent))


def main():
    log_path = start_logging("master_pi", BASE_DIR)
    print(f"(log: {log_path})")

    parser = argparse.ArgumentParser(description="Money Badger — daily fetch/push (Pi)")
    parser.add_argument("--dry-run", action="store_true", help="fetch without writing CSVs and without pushing")
    args = parser.parse_args()

    import datetime
    print(f"=== Money Badger sync (Pi) — {datetime.date.today().isoformat()} ===\n")

    print("=== Step 0: Enable Banking fetch ===\n")
    fetch_ok = run_fetch(dry_run=args.dry_run)
    print()

    print("=== Step 0b: Corrections from the app → main.py learn ===\n")
    run_learn_from_budget(dry_run=args.dry_run)
    print()

    if args.dry_run:
        print("[dry-run] Push skipped.")
        return

    print("=== Step 3: Push transactions to the app ===\n")
    run_push_actuals()
    print()

    print("=== Step 4: Match orders from email ===\n")
    run_allegro_match()

    try:
        summary = json.loads(SUMMARY_FILE.read_text()) if SUMMARY_FILE.exists() else {}
    except Exception:
        summary = {}
    summary_accounts = summary.get("accounts", [])
    summary_failed = summary.get("failed_accounts", [])
    summary_reasons = summary.get("failed_reasons", {})
    today_str = datetime.date.today().isoformat()

    def _n(n, word):
        return f"{n} {word}" if n == 1 else f"{n} {word}s"

    def _account_blocks():
        blocks = []
        for acc in summary_accounts:
            txs = acc["transactions"]
            if not txs:
                blocks.append(f"{acc['name']} (0 transactions).")
                continue
            block = [f"{acc['name']} ({_n(len(txs), 'transaction')}):"]
            for tx in txs:
                block.append(f"  {tx['tx_type']} | {tx['amount']:+.2f} PLN | {tx['description']}")
            blocks.append("\n".join(block))
        return blocks

    if fetch_ok:
        mark_success("master_pi", BASE_DIR)
        if not _should_notify(summary, today_str):
            print("\nNothing new — skipping the Telegram notification.")
            return
        checkme_count = _get_checkme_count()
        blocks = _account_blocks()
        body = "\n\n".join(blocks) if blocks else "(no new transactions)"

        if summary_failed:
            ok_names = ", ".join(a["name"] for a in summary_accounts) or "(none)"
            failed_lines = "\n".join(
                f"{name} - {summary_reasons.get(name, 'unknown reason')}" for name in summary_failed
            )
            msg = (f"⚠️ Money Badger — sync partial ({today_str})\n\n"
                   f"OK: {ok_names}\n\n"
                   f"Failed:\n{failed_lines}\n\n"
                   f"{body}\n\n"
                   f"CHECK ME: {_n(int(checkme_count), 'transaction') if checkme_count.isdigit() else checkme_count}")
        else:
            msg = (f"✅ Money Badger — sync OK ({today_str})\n\n{body}\n\n"
                   f"CHECK ME: {_n(int(checkme_count), 'transaction') if checkme_count.isdigit() else checkme_count}")
        send_telegram(msg)
    else:
        print(f"\n❌❌❌ SYNC INCOMPLETE — the fetch or categorization failed for some or"
              f" all accounts. No success marker was written, so the watchdog will try"
              f" again. See logs/sync_{today_str}.log.")
        if summary_failed:
            failed_lines = "\n".join(
                f"{name} - {summary_reasons.get(name, 'unknown reason')}" for name in summary_failed
            )
        else:
            failed_lines = "(no accounts identified — check the log)"
        send_telegram(f"❌ Money Badger — sync FAILED ({today_str})\n\n{failed_lines}\n\n"
                      f"Check logs/sync_{today_str}.log for details.")


if __name__ == "__main__":
    # Before start_logging(), so a refused second run leaves no trace in the log
    # and exits 0 — the watchdog must not be reported as failed for standing down.
    _lock = acquire_lock()
    if _lock is None:
        print("master_pi.py is already running — exiting without a second pass.")
        sys.exit(0)
    main()
