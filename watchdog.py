#!/usr/bin/env python3
"""
watchdog.py – sprawdza, czy dzisiejszy master_pi.py zakończył się sukcesem
(marker logs/.success_master_pi_{data}); jeśli nie, odpala go ponownie.

Uruchamiany przez home-badger-sync-watchdog.timer o 14:30/17:30/23:30 —
łapie przypadki gdy 10:30 nie doszło do końca (np. aplikacja chwilowo
niedostępne). Bezpieczne do wielokrotnego odpalenia tego samego dnia —
fetch/push są idempotentne (dedup po hash/eb_id).
"""
import subprocess
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).parent

import env_file

env_file.load()

from logging_setup import start_logging

MARKER = BASE_DIR / "logs" / f".success_master_pi_{datetime.now():%Y-%m-%d}"


def main():
    log_path = start_logging("watchdog", BASE_DIR)
    print(f"(log: {log_path})")

    if MARKER.exists():
        print(f"OK — {MARKER.name} istnieje, dzisiejszy master_pi.py już się powiódł.")
        return

    print(f"Brak {MARKER.name} — dzisiejszy przebieg nie doszedł do końca. Ponawiam master_pi.py...\n")
    subprocess.run([sys.executable, str(BASE_DIR / "master_pi.py")])


if __name__ == "__main__":
    main()
