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

Run daily by money-badger-sync.timer.
"""

import argparse
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
    """Krok 0b: pobierz korekty z aplikacji → Corrections/ → main.py learn."""
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
    """Krok 3: pushuj transakcje do aplikacji (zwykle ten sam host)."""
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

    parser = argparse.ArgumentParser(description="Home Badger — codzienny fetch/push (Pi)")
    parser.add_argument("--dry-run", action="store_true", help="fetch bez zapisu CSV, bez pushu")
    args = parser.parse_args()

    import datetime
    print(f"=== Home Badger sync (Pi) — {datetime.date.today().isoformat()} ===\n")

    print("=== Krok 0: Enable Banking fetch ===\n")
    fetch_ok = run_fetch(dry_run=args.dry_run)
    print()

    print("=== Krok 0b: Korekty z aplikacji → main.py learn ===\n")
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
    main()
