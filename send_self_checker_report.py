"""Send the report that sync/self_checker.py wrote overnight.

Split into two timers on purpose: the audit can run late at night while the
notification waits until morning, so nothing buzzes in the middle of it.
"""
from pathlib import Path

from notify_telegram import send_telegram

REPORT_FILE = Path(__file__).parent / ".self_checker_report.txt"

if __name__ == "__main__":
    if not REPORT_FILE.exists():
        print("Nothing to send — a clean night.")
    else:
        msg = REPORT_FILE.read_text(encoding="utf-8")
        if send_telegram(msg):
            REPORT_FILE.unlink()
            print("Report sent and removed.")
        else:
            # ponytail: plik zostaje, ale jutrzejszy self_checker.py i tak go
            # the next audit overwrites it with fresher data before this sender
            # runs again, so one Telegram outage costs one morning report rather
            # than building a backlog. Add a retry queue only if that starts to hurt.
            print("Sending failed — try by hand: python3 send_self_checker_report.py")
