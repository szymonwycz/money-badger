#!/usr/bin/env python3
"""
watchdog.py – did today's master_pi.py finish? The run leaves a marker at
logs/.success_master_pi_{date}; without one, run it again.

Run late by money-badger-sync-watchdog.timer when the configured transaction
attempt budget leaves a spare slot. It catches a day in which no scheduled pass
finished — the app briefly down, the network gone. It does not retry a failed
late pass after an earlier pass succeeded. That pass's rows remain reachable
through the next day's overlapping fetch window. Fetch and push are idempotent,
deduplicating on hash/eb_id.
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
        print(f"OK — {MARKER.name} exists, today's run already succeeded.")
        return

    print(f"No {MARKER.name} — today's run did not finish. Running master_pi.py again...\n")
    subprocess.run([sys.executable, str(BASE_DIR / "master_pi.py")])


if __name__ == "__main__":
    main()
