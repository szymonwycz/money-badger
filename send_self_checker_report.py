"""Wysyła (o 08:30, systemd timer) raport zapisany w nocy przez
sync/self_checker.py — rozdzielone celowo, żeby audyt mógł liczyć się
o 23:45 bez budzenia powiadomieniem w środku nocy."""
from pathlib import Path

from notify_telegram import send_telegram

REPORT_FILE = Path(__file__).parent / ".self_checker_report.txt"

if __name__ == "__main__":
    if not REPORT_FILE.exists():
        print("Brak raportu do wysłania — czysta noc.")
    else:
        msg = REPORT_FILE.read_text(encoding="utf-8")
        if send_telegram(msg):
            REPORT_FILE.unlink()
            print("Wysłano i usunięto raport.")
        else:
            # ponytail: plik zostaje, ale jutrzejszy self_checker.py i tak go
            # nadpisze o 23:45 (nowszymi danymi) zanim ten sender znów ruszy —
            # jednorazowa awaria Telegrama = jeden pominięty poranny raport,
            # nie kolejka. Dodać retry/kolejkę dopiero jeśli to zacznie boleć.
            print("Wysyłka nieudana — spróbuj ręcznie: python3 send_self_checker_report.py")
