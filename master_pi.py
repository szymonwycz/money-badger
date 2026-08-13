#!/usr/bin/env python3
"""
master_pi.py – codzienny orchestrator fetch/push dla Money Badger.

Kroki: 0 fetch z banku → 0b nauka reguł z korekt → 3 push do appki → 4 Allegro.

Salda kont (dawny Krok 0c) NIE są już częścią tego pipeline'u — to tylko
kontrola rozbieżności (aplikacja liczy saldo z transakcji, nie z EB), więc
przeniesione na osobny, cotygodniowy timer (home-badger-balance-check.timer,
sobota 02:30) zamiast bić w limit ASPSP przy każdym sync/watchdog hopie.

Tryb: python3 master_pi.py [--dry-run]
Uruchamiany codziennie o 10:30 przez systemd timer (home-badger-sync.timer).
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
ALLEGRO_SCRIPT = BASE_DIR / "sync" / "allegro_match.py"

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


# Powody skipów, które fetch()/get_balances() zwracają same z siebie (patrz
# sync/banks/enable_banking.py) — żaden z nich nie naprawi się przez odczekanie
# 60s i ponowienie: limit ASPSP wraca dopiero jutro, a wygasła/brakująca sesja
# wymaga ręcznego `fetch-setup`. Retry ma sens tylko dla realnie przejściowych
# błędów (Anthropic API, aplikacja chwilowo nie odpowiada podczas kategoryzacji
# w cmd_fetch) — te trafiają do failed_reasons z INNYM tekstem (treść wyjątku),
# więc rozróżnienie po treści stringa wystarcza.
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
    """Krok 0: pobierz transakcje przez Enable Banking. Pomija cicho (zwraca True)
    jeśli EB nie jest skonfigurowany — to nie jest błąd, tylko "nic do zrobienia".
    Zwraca False jeśli fetch/kategoryzacja faktycznie się nie powiodły (nawet dla
    części kont) — main() używa tego, żeby NIE oznaczać runu jako udany, więc
    watchdog (home-badger-sync-watchdog.timer) faktycznie ponowi próbę o 14:30
    zamiast cicho uznać dzień za załatwiony."""
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
            print("\nEnable Banking fetch nieudany — wyłącznie limit ASPSP/sesja (nie coś"
                  " przejściowego), retry nic by nie dał. Pomijam, spróbuje kolejny watchdog hop.")
            return False
        print("\nOSTRZEŻENIE: Enable Banking fetch nieudany — retry za 60s...")
        time.sleep(60)
        result = subprocess.run(cmd, cwd=str(SYNC_SCRIPT.parent))
        if result.returncode != 0:
            print("\n❌ Enable Banking fetch nieudany (2/2) — część lub wszystkie konta"
                  " NIE zostały przetworzone. Checkpoint dla nich jest bezpieczny"
                  " (nic nie zgubione) — spróbuje ponownie watchdog o 14:30 lub jutrzejszy sync.")
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
            print("  Ostrzeżenie: push_actuals (pull korekt) zakończył się błędem.")
            return

        new_csvs = [f for f in corrections_dir.glob("budget_corrections_*.csv")]
        if not new_csvs:
            return

        print("  Uczę się z korekt (main.py learn)...")
        cmd = [sys.executable, str(SYNC_SCRIPT), "learn"]
        if dry_run:
            cmd.append("--dry-run")
        learn = subprocess.run(cmd, cwd=str(SYNC_SCRIPT.parent))
        if learn.returncode != 0:
            # Return code used to be ignored: learn could crash and the corrections
            # were flagged synced anyway, so the rules never learned them and nothing
            # ever retried — the flag is one-way.
            print("  Ostrzeżenie: main.py learn zakończył się błędem — "
                  "korekty NIE zostaną oznaczone, ponowię przy następnym runie.")
            return

        if not dry_run:
            # Mark only the correction ids that actually made it into these CSVs.
            ids = []
            for csv_path in new_csvs:
                ids_file = csv_path.with_suffix(".ids")
                if ids_file.exists():
                    ids += [int(i) for i in ids_file.read_text().split(",") if i.strip()]
            if not ids:
                print("  Brak listy id korekt — pomijam oznaczanie (zostaną ponowione).")
                return

            try:
                budget_client.post("/api/corrections/mark-synced", {"ids": ids}, timeout=5)
            except Exception:
                pass  # aplikacja chwilowo niedostępna — korekty zostaną ponowione przy następnym runie

    except Exception as e:
        print(f"  Ostrzeżenie: błąd integracji z aplikacją: {e}")


def run_push_actuals():
    """Krok 3: pushuj transakcje do aplikacji (zwykle ten sam host)."""
    if not PUSH_SCRIPT.exists():
        return
    subprocess.run([sys.executable, str(PUSH_SCRIPT)], cwd=str(BASE_DIR))


def run_allegro_match():
    """Krok 4: dopasuj maile z zakupami Allegro do transakcji CHECK ME.
    Pomija cicho jeśli GMAIL_ADDRESS/GMAIL_APP_PASSWORD nie są skonfigurowane
    (patrz sync/allegro_match.py) — nieblokujące, tak jak inne opcjonalne kroki."""
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
        print("[dry-run] Push pominięty.")
        return

    print("=== Krok 3: Push transakcji do aplikacji ===\n")
    run_push_actuals()
    print()

    print("=== Krok 4: Dopasowanie zakupów Allegro (Gmail) ===\n")
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
        print(f"\n❌❌❌ SYNC NIEPEŁNY — fetch/kategoryzacja nie powiodła się dla części lub"
              f" wszystkich kont. Brak markera sukcesu → watchdog ponowi próbę o 14:30."
              f" Sprawdź logs/sync_{today_str}.log.")
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
