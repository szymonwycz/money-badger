#!/usr/bin/env python3
"""
push_actuals.py — czyta Export/ CSVy i pushuje transakcje do aplikacji.
Uruchamiany przez master.py po każdym imporcie.
"""
import csv
import glob
import hashlib
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))
import env_file

env_file.load()

from logging_setup import start_logging
import budget_client
EXPORT_DIR = BASE_DIR / "Export"

STATE_FILE = BASE_DIR / ".push_actuals_state.json"


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"pushed_hashes": []}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def post_json(path, data):
    return budget_client.post(path, data, timeout=10)


def get_json(path):
    return budget_client.get(path)


def parse_amount(s):
    """'1 309,00 zł' → 1309.0"""
    s = s.strip()
    negative = s.startswith("(") and s.endswith(")")
    s = re.sub(r"[()zł\s]", "", s).replace(",", ".").replace(" ", "")
    try:
        val = float(s)
        return -val if negative else val
    except ValueError:
        return 0.0


def parse_date(s):
    """'29/06/2026, 01:00' → '29/06/2026'"""
    return s.split(",")[0].strip()


def tx_hash(date, amount, account, description):
    key = f"{date}|{amount}|{account}|{description}"
    return hashlib.sha1(key.encode()).hexdigest()


def read_csv(path):
    txs = []
    with open(path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f, delimiter=";")
        for row in reader:
            tx_type = (row.get("Transaction Type") or "").strip()
            if tx_type in ("Opening Balance", "Balance Adjustment"):
                continue
            category = (row.get("Category") or "").strip()
            if not category and tx_type != "Money Transfer":
                continue

            date       = parse_date(row.get("Date", ""))
            amount     = parse_amount(row.get("Amount", "0"))
            acct       = (row.get("Account") or "").strip()
            account_to = (row.get("Account (to)") or "").strip()
            desc       = (row.get("Description") or "").strip()

            h = tx_hash(date, amount, acct, desc)
            txs.append({
                "date":        date,
                "amount":      amount,
                "account":     acct,
                "account_to":  account_to,
                "description": desc,
                "category":    category,
                "tx_type":     tx_type,
                "hash":        h,
            })
    return txs


def pull_corrections(corrections_dir: Path):
    """Pobierz niezsynced korekty z aplikacji → zapisz CSV → zwróć liczbę korekt."""
    try:
        content, headers = budget_client.request(
            "/api/corrections?synced=0", timeout=10, with_headers=True)
        # exactly which correction rows this CSV covers, so only these get
        # flagged synced later — a correction made while `learn` runs must not
        # be marked as handled when it never made it into any CSV
        ids = headers.get("X-Correction-Ids", "")
    except Exception as e:
        print(f"  [push_actuals] Nie udało się pobrać korekt: {e}")
        return 0

    # Sprawdź czy są jakieś wiersze (poza nagłówkiem)
    lines = [l for l in content.strip().splitlines() if l.strip()]
    if len(lines) <= 1:
        print("  [push_actuals] Brak nowych korekt.")
        return 0

    corrections_dir.mkdir(parents=True, exist_ok=True)
    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = corrections_dir / f"budget_corrections_{ts}.csv"
    dest.write_text(content, encoding="utf-8")
    (corrections_dir / f"budget_corrections_{ts}.ids").write_text(ids, encoding="utf-8")
    print(f"  [push_actuals] Zapisano {len(lines)-1} korekt → {dest.name}")
    return len(lines) - 1


def mark_corrections_synced():
    try:
        post_json("/api/corrections/mark-synced", {})
        print("  [push_actuals] Korekty oznaczone jako synced.")
    except Exception as e:
        print(f"  [push_actuals] Błąd oznaczania korekt: {e}")


def push(corrections_dir: Path = None):
    # 1. Pobierz korekty (jeśli podano ścieżkę)
    n_corrections = 0
    if corrections_dir:
        n_corrections = pull_corrections(corrections_dir)

    # 2. Pushuj transakcje
    state        = load_state()
    pushed_set   = set(state.get("pushed_hashes", []))
    csv_files    = sorted(EXPORT_DIR.glob("moneypro_*.csv"))

    all_txs = []
    for f in csv_files:
        all_txs.extend(read_csv(f))

    # Disambiguate rows that hash-collide with each other in this batch — e.g. an
    # Income and an Expense on the same day/account/description with the same
    # absolute amount (tx_hash doesn't include tx_type). Without this, INSERT OR
    # IGNORE silently drops the second one as a false "duplicate". Deterministic
    # per (date, amount, account, description) occurrence order, so re-reading
    # the same CSVs later reproduces the same hashes.
    seen_counts = {}
    for tx in all_txs:
        h = tx["hash"]
        n = seen_counts.get(h, 0)
        if n:
            tx["hash"] = hashlib.sha1(f"{h}|{n}".encode()).hexdigest()
        seen_counts[h] = n + 1

    new_txs = [tx for tx in all_txs if tx["hash"] not in pushed_set]
    if not new_txs:
        print("  [push_actuals] Brak nowych transakcji.")
    else:
        try:
            result = post_json("/api/transactions/bulk", new_txs)
            inserted = result.get("inserted", 0)
            # Mark only what the server confirmed it stored. Marking every candidate
            # meant a row the server skipped (bad date, constraint violation) was never
            # retried by any later run — it just vanished from the budget.
            accepted = set(result.get("accepted") or [])
            print(f"  [push_actuals] Pushowano {inserted} nowych transakcji ({len(new_txs)} kandydatów).")

            rejected = [tx for tx in new_txs if tx["hash"] not in accepted]
            if rejected:
                print(f"  [push_actuals] UWAGA: serwer odrzucił {len(rejected)} wierszy — "
                      f"zostaną ponowione przy następnym runie:")
                for tx in rejected[:10]:
                    print(f"    - {tx['date']} {tx['amount']} {tx['account']} {tx['description'][:40]}")

            pushed_set |= accepted
            state["pushed_hashes"] = list(pushed_set)
            save_state(state)
        except budget_client.HTTPError as e:
            # HTTPError subclasses URLError, so it used to be reported as the app
            # being unreachable — a 413 (batch over nginx's body limit) then repeated
            # every day with a misleading message and nothing ever got through.
            print(f"  [push_actuals] aplikacja odrzuciła żądanie: HTTP {e.code} {e.reason}. "
                  f"Nic nie oznaczono jako wysłane — ponowię przy następnym runie.")
            return n_corrections
        except budget_client.URLError as e:
            print(f"  [push_actuals] aplikacja niedostępna: {e}. Pomijam.")
            return n_corrections

    return n_corrections


if __name__ == "__main__":
    log_path = start_logging("push_actuals", BASE_DIR)
    print(f"(log: {log_path})")

    corrections_dir = None
    if len(sys.argv) > 1:
        corrections_dir = Path(sys.argv[1])
    push(corrections_dir)
