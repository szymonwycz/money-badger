#!/usr/bin/env python3
"""Self-check for sync/self_checker.py — run directly, no framework/fixtures.

python3 sync/test_self_checker.py

One test per check_*() function: a synthetic .bank_sync_state.json/log/
budget.db per test, asserting both the "should flag" and "should not flag"
case where the check has to distinguish real problems from noise (an old
stale sample vs. the latest weekly balance check, unrelated duplicate
Expense rows, etc).
"""
import json
import sqlite3
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import self_checker


def _tmp_root():
    root = Path(tempfile.mkdtemp())
    (root / "logs").mkdir()
    (root / "raw_pulls").mkdir()
    self_checker.ROOT_DIR = root
    self_checker.BASE_DIR = root  # config.json / .bank_sync_state.json look here in tests
    self_checker.DB_PATH = root / "budget.db"
    return root


def _make_db(root):
    con = sqlite3.connect(str(root / "budget.db"))
    con.execute("""
        CREATE TABLE transactions (
            id INTEGER PRIMARY KEY, date TEXT, amount REAL, account TEXT,
            category_id INTEGER, income_category_id INTEGER,
            tx_type TEXT DEFAULT 'Expense', ignored INTEGER DEFAULT 0
        )
    """)
    con.commit()
    return con


def test_stale_fetch_flags_old_last_fetch():
    root = _tmp_root()
    # State keys are full IBANs, so the config has to say which country.
    cfg = {"own_ibans": ["1111"], "account_names": {"1111": "Checking"},
           "enable_banking": {"default_aspsp": {"country": "PL"}}}
    old = (date.today() - timedelta(days=3)).isoformat()
    (root / ".bank_sync_state.json").write_text(json.dumps({"last_fetch": {"PL1111": old}}))
    findings = self_checker.check_stale_fetch(cfg)
    assert any("Checking" in f for f in findings), findings

    recent = date.today().isoformat()
    (root / ".bank_sync_state.json").write_text(json.dumps({"last_fetch": {"PL1111": recent}}))
    findings = self_checker.check_stale_fetch(cfg)
    assert findings == [], findings


def test_yesterdays_checkpoint_is_the_healthy_state_not_a_finding():
    """A successful sync checkpoints *yesterday* — it only fetches fully booked
    days — so one day of lag is normal, not stale."""
    root = _tmp_root()
    cfg = {"own_ibans": ["1111"], "account_names": {"1111": "Checking"},
           "enable_banking": {"default_aspsp": {"country": "PL"}}}
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    (root / ".bank_sync_state.json").write_text(json.dumps({"last_fetch": {"PL1111": yesterday}}))
    (root / "logs").mkdir(exist_ok=True)
    (root / "logs" / f".success_master_pi_{date.today().isoformat()}").touch()
    assert self_checker.check_stale_fetch(cfg) == []


def test_two_days_is_only_stale_once_todays_sync_has_run():
    """Between midnight and the daily run the checkpoint is two days back and
    nothing is wrong yet. Running the audit by hand in the morning used to
    report every account as stale."""
    root = _tmp_root()
    cfg = {"own_ibans": ["1111"], "account_names": {"1111": "Checking"},
           "enable_banking": {"default_aspsp": {"country": "PL"}}}
    two_days = (date.today() - timedelta(days=2)).isoformat()
    (root / ".bank_sync_state.json").write_text(json.dumps({"last_fetch": {"PL1111": two_days}}))
    (root / "logs").mkdir(exist_ok=True)

    # Sync hasn't run yet today — expected, so quiet.
    assert self_checker.check_stale_fetch(cfg) == []

    # It has run, and the checkpoint still didn't move: that is a real problem.
    (root / "logs" / f".success_master_pi_{date.today().isoformat()}").touch()
    assert any("Checking" in f for f in self_checker.check_stale_fetch(cfg))


def test_allegro_is_not_flagged_before_todays_sync_has_run():
    """Allegro matching is the last step of the sync, so before the daily run
    "no log for today" says nothing about whether it works."""
    root = _tmp_root()
    (root / "logs").mkdir(exist_ok=True)
    yesterday = date.today() - timedelta(days=1)
    (root / "logs" / f"allegro_match_{yesterday.isoformat()}.log").write_text("START allegro_match\nok\n")

    assert self_checker.check_allegro_backlog() == []

    (root / "logs" / f".success_master_pi_{date.today().isoformat()}").touch()
    findings = self_checker.check_allegro_backlog()
    assert any(date.today().isoformat() in f for f in findings), findings


def _write_diff_log(root, day, name, diff):
    log = root / "logs" / f"sync_{day.isoformat()}.log"
    log.write_text(f"[00:00:00]   ⚠ {name}: app=100.00 PLN, bank=200.00 PLN "
                    f"(różnica {diff:+.2f} zł — sprawdź)\n")


def test_diff_parser_reads_every_wording_this_log_has_ever_had():
    """Balance-check logs live for 90 days and the line has been rewritten twice:
    budget.lan -> app, and Polish -> English with a configurable currency. Every
    shape must still parse, or drift detection goes quiet without failing a test.
    """
    lines = [
        # oldest: pre-rename, Polish, hardcoded PLN
        "[00:00:00]   ⚠ Main: budget.lan=100.00 PLN, bank=200.00 PLN "
        "(różnica +100.00 zł — sprawdź)",
        # Polish, after the budget.lan -> app rename
        "[00:00:00]   ⚠ Main: app=100.00 PLN, bank=200.00 PLN "
        "(różnica +100.00 zł — sprawdź)",
        # current: English, currency from settings
        "[00:00:00]   ⚠ Main: app=100.00 PLN, bank=200.00 PLN "
        "(difference +100.00 PLN — check for missing or duplicated transactions)",
        # a household running in euro
        "[00:00:00]   ⚠ Main: app=100.00 €, bank=200.00 € "
        "(difference +100.00 € — check for missing or duplicated transactions)",
        # app unreachable at sync time: currency() returns "" and the symbol is absent
        "[00:00:00]   ⚠ Main: app=100.00 , bank=200.00  "
        "(difference +100.00  — check for missing or duplicated transactions)",
    ]
    for line in lines:
        m = self_checker.DIFF_RE.search(line)
        assert m and m.group("name") == "Main" and float(m.group("diff")) == 100.0, line


def test_balance_discrepancy_uses_latest_weekly_check_only():
    """Balance check is weekly now (money-badger-balance-check.timer) — only one
    sample exists between runs, so the check must look at the MOST RECENT
    sync_*.log with a diff line and judge it against tolerance alone, not
    compare it to older samples (there's no day-to-day trend at weekly cadence)."""
    root = _tmp_root()
    today = date.today()
    _write_diff_log(root, today, "OverTolerance", -100.0)
    findings = self_checker.check_balance_discrepancy()
    assert any("OverTolerance" in f for f in findings), findings

    root2 = _tmp_root()
    last_saturday = date.today() - timedelta(days=3)
    _write_diff_log(root2, last_saturday, "WithinTolerance", -1.0)
    findings = self_checker.check_balance_discrepancy()
    assert findings == [], findings


def test_balance_discrepancy_flags_when_timer_stopped_running():
    root = _tmp_root()
    findings = self_checker.check_balance_discrepancy()
    assert any("timer broken" in f for f in findings), findings


def test_duplicate_income_balance_adjust_sql():
    root = _tmp_root()
    con = _make_db(root)
    con.execute("INSERT INTO transactions (date, amount, account, tx_type) VALUES "
                "('2026-07-10', 100.0, 'Acc', 'Income')")
    con.execute("INSERT INTO transactions (date, amount, account, tx_type) VALUES "
                "('2026-07-10', 100.0, 'Acc', 'Balance Adjust')")
    con.commit()
    con.close()
    findings = self_checker.check_duplicate_income_balance_adjust()
    assert len(findings) == 1, findings

    root2 = _tmp_root()
    con = _make_db(root2)
    con.execute("INSERT INTO transactions (date, amount, account, tx_type) VALUES "
                "('2026-07-10', 50.0, 'Acc', 'Expense')")
    con.execute("INSERT INTO transactions (date, amount, account, tx_type) VALUES "
                "('2026-07-10', 50.0, 'Acc', 'Expense')")
    con.commit()
    con.close()
    findings = self_checker.check_duplicate_income_balance_adjust()
    assert findings == [], findings


def test_orphan_raw_pulls_flags_missing_commit():
    import os
    import time
    root = _tmp_root()
    con = _make_db(root)
    con.commit()
    con.close()
    cfg = {"account_names": {"1111": "Checking"}}

    raw = {"transactions": [
        {"transaction_amount": {"amount": "10.00"}},
        {"transaction_amount": {"amount": "20.00"}},
    ]}
    f = root / "raw_pulls" / "1111_2026-07-01_2026-07-01_20260701T000000.json"
    f.write_text(json.dumps(raw))
    old_time = time.time() - 86400  # a day ago — older than the 6h threshold
    os.utime(f, (old_time, old_time))
    findings = self_checker.check_orphan_raw_pulls(cfg)
    assert any("Checking" in x for x in findings), findings

    con = sqlite3.connect(str(root / "budget.db"))
    con.execute("INSERT INTO transactions (date, amount, account, tx_type) VALUES "
                "('2026-07-01', 10.0, 'Checking', 'Expense')")
    con.commit()
    con.close()
    findings = self_checker.check_orphan_raw_pulls(cfg)
    assert findings == [], findings


def test_checkme_backlog_respects_ignored_and_type_filters():
    root = _tmp_root()
    con = _make_db(root)
    old = (date.today() - timedelta(days=10)).isoformat()
    con.execute("INSERT INTO transactions (date, amount, account, tx_type, ignored) VALUES "
                f"('{old}', 10.0, 'Acc', 'Expense', 0)")
    con.commit()
    con.close()
    findings = self_checker.check_checkme_backlog()
    assert len(findings) == 1, findings

    root2 = _tmp_root()
    con = _make_db(root2)
    con.execute("INSERT INTO transactions (date, amount, account, tx_type, ignored) VALUES "
                f"('{old}', 10.0, 'Acc', 'Expense', 1)")  # ignored -> excluded
    con.execute("INSERT INTO transactions (date, amount, account, tx_type, ignored) VALUES "
                f"('{old}', 10.0, 'Acc', 'Money Transfer', 0)")  # excluded type
    con.commit()
    con.close()
    findings = self_checker.check_checkme_backlog()
    assert findings == [], findings


def _write_run(log_path, pid, body):
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"{'─' * 10}\n[2026-07-16 00:00:00] START allegro_match (pid={pid}, args=[])\n{'─' * 10}\n{body}\n")


def test_allegro_backlog_only_checks_last_run_of_the_day():
    root = _tmp_root()
    today, yesterday = date.today(), date.today() - timedelta(days=1)
    # Today is only examined once the day's sync has succeeded.
    (root / "logs" / f".success_master_pi_{today.isoformat()}").touch()
    _write_run(root / "logs" / f"allegro_match_{yesterday.isoformat()}.log", 0, "[allegro] OK")
    log_path = root / "logs" / f"allegro_match_{today.isoformat()}.log"
    # An earlier ad-hoc/manual run failed (bad IMAP creds from a botched debug
    # session), but the LAST run of the day was clean — must not flag, or one
    # bad manual invocation would falsely taint the whole day.
    _write_run(log_path, 1, "[orders] IMAP error: bad creds")
    _write_run(log_path, 2, "[allegro] OK, matched 2 transactions")
    findings = self_checker.check_allegro_backlog()
    assert findings == [], findings

    root2 = _tmp_root()
    (root2 / "logs" / f".success_master_pi_{today.isoformat()}").touch()
    _write_run(root2 / "logs" / f"allegro_match_{yesterday.isoformat()}.log", 0, "[allegro] OK")
    log_path2 = root2 / "logs" / f"allegro_match_{today.isoformat()}.log"
    _write_run(log_path2, 1, "[allegro] OK, matched 2 transactions")
    _write_run(log_path2, 2, "[orders] IMAP error: bad creds")
    findings = self_checker.check_allegro_backlog()
    assert len(findings) >= 1, findings


if __name__ == "__main__":
    test_stale_fetch_flags_old_last_fetch()
    test_yesterdays_checkpoint_is_the_healthy_state_not_a_finding()
    test_two_days_is_only_stale_once_todays_sync_has_run()
    test_allegro_is_not_flagged_before_todays_sync_has_run()
    test_diff_parser_still_reads_logs_from_before_the_rename()
    test_balance_discrepancy_uses_latest_weekly_check_only()
    test_balance_discrepancy_flags_when_timer_stopped_running()
    test_duplicate_income_balance_adjust_sql()
    test_orphan_raw_pulls_flags_missing_commit()
    test_checkme_backlog_respects_ignored_and_type_filters()
    test_allegro_backlog_only_checks_last_run_of_the_day()
    print("OK — all self_checker tests passed")
