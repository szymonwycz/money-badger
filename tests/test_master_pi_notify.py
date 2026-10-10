#!/usr/bin/env python3
"""Self-check for master_pi._should_notify() — run directly, no framework.

PYTHONPATH=.. python3 test_master_pi_notify.py

The sync may run several times a day, one pass per incoming settlement session
at the bank. Telegram should speak up only when there are new transactions or
when an account failed — otherwise most passes send an empty message.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import master_pi

TODAY = "2026-09-10"


def test_new_transactions_notify():
    summary = {"date": TODAY, "failed_accounts": [],
               "accounts": [{"name": "Checking", "transactions": [{"amount": -9.02}]}]}
    assert master_pi._should_notify(summary, TODAY) is True


def test_empty_run_is_silent():
    summary = {"date": TODAY, "failed_accounts": [],
               "accounts": [{"name": "Checking", "transactions": []},
                            {"name": "Savings", "transactions": []}]}
    assert master_pi._should_notify(summary, TODAY) is False


def test_three_intraday_passes_notify_new_silent_new():
    def summary(transactions):
        return {"date": TODAY, "failed_accounts": [],
                "accounts": [{"name": "Checking", "transactions": transactions}]}

    assert master_pi._should_notify(summary([{"amount": -10.0}]), TODAY) is True
    assert master_pi._should_notify(summary([]), TODAY) is False
    assert master_pi._should_notify(summary([{"amount": 25.0}]), TODAY) is True


def test_failed_account_notifies_even_without_transactions():
    summary = {"date": TODAY, "failed_accounts": ["Savings"],
               "failed_reasons": {"Savings": "ASPSP daily limit reached"},
               "accounts": [{"name": "Checking", "transactions": []}]}
    assert master_pi._should_notify(summary, TODAY) is True


def test_window_cap_warning_notifies_without_failed_account():
    summary = {"date": TODAY, "failed_accounts": [], "accounts": [],
               "window_warnings": {
                   "Checking": "fetch window capped at 30 days; dates 2032-07-01..2032-07-10 were not requested"
               }}
    assert master_pi._should_notify(summary, TODAY) is True
    block = master_pi._window_warning_block(summary)
    assert "Permanent bank-data gap" in block
    assert "Checking" in block and "2032-07-01..2032-07-10" in block


def test_stale_summary_is_silent():
    """Enable Banking not configured -> run_fetch() returns before rewriting the
    summary; yesterday's transactions must not go out a second time."""
    summary = {"date": "2026-09-09", "failed_accounts": [],
               "accounts": [{"name": "Checking", "transactions": [{"amount": -9.02}]}]}
    assert master_pi._should_notify(summary, TODAY) is False


def test_missing_summary_is_silent():
    assert master_pi._should_notify({}, TODAY) is False


if __name__ == "__main__":
    test_new_transactions_notify()
    test_empty_run_is_silent()
    test_three_intraday_passes_notify_new_silent_new()
    test_failed_account_notifies_even_without_transactions()
    test_window_cap_warning_notifies_without_failed_account()
    test_stale_summary_is_silent()
    test_missing_summary_is_silent()
    print("OK — notification gate tests passed")
