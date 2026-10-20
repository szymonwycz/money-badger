#!/usr/bin/env python3
"""Self-check for master_pi._only_non_retryable_failures() — run directly, no
framework/fixtures.

python3 test_master_pi_retry.py

Regression coverage for the 2026-07-16 retry-storm fix: run_fetch() used to
blindly sleep 60s and retry the whole fetch whenever ANY account failed —
even when the failure was ASPSP daily limit or an expired/missing EB session,
neither of which can possibly resolve in 60 seconds. Now it only retries when
at least one failure looks transient (i.e. isn't one of the known dead-end
reasons).
"""
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent))
import master_pi


def _use_tmp_summary():
    tmp = Path(tempfile.mkdtemp()) / ".last_fetch_summary.json"
    master_pi.SUMMARY_FILE = tmp
    return tmp


def test_all_quota_failures_skips_retry():
    summary_file = _use_tmp_summary()
    summary_file.write_text(json.dumps({
        "failed_reasons": {"Main": "ASPSP daily limit reached", "Partner": "ASPSP daily limit reached"},
    }))
    assert master_pi._only_non_retryable_failures() is True


def test_mixed_session_and_quota_failures_skips_retry():
    summary_file = _use_tmp_summary()
    summary_file.write_text(json.dumps({
        "failed_reasons": {"Main": "ASPSP daily limit reached", "Partner": "session expired (401)"},
    }))
    assert master_pi._only_non_retryable_failures() is True


def test_unknown_failure_reason_allows_retry():
    """A real exception during categorization (Anthropic API, app down)
    lands in failed_reasons with the exception text, not one of the fixed
    dead-end strings — that's exactly the case retry exists for."""
    summary_file = _use_tmp_summary()
    summary_file.write_text(json.dumps({
        "failed_reasons": {"Checking": "Connection refused (app)"},
    }))
    assert master_pi._only_non_retryable_failures() is False


def test_missing_summary_file_allows_retry():
    master_pi.SUMMARY_FILE = Path(tempfile.mkdtemp()) / "missing.json"
    assert master_pi._only_non_retryable_failures() is False


def test_run_fetch_targets_only_failed_accounts_on_retry(monkeypatch, tmp_path):
    """A fresh per-account summary narrows the second subprocess instead of
    repeating the complete bank fetch."""
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"enable_banking": {"app_id": "offline"}}))
    summary = tmp_path / ".last_fetch_summary.json"
    monkeypatch.setattr(master_pi, "SYNC_CFG", cfg)
    monkeypatch.setattr(master_pi, "SUMMARY_FILE", summary)
    commands = []

    def fake_run(cmd, cwd):
        commands.append(cmd)
        if len(commands) == 1:
            summary.write_text(json.dumps({
                "date": master_pi.time.strftime("%Y-%m-%d"),
                "accounts": [
                    {"name": f"Account {index}", "transactions": []}
                    for index in range(1, 5)
                ],
                "failed_accounts": ["Account 5"],
                "failed_reasons": {"Account 5": "400 Client Error"},
            }))
            return SimpleNamespace(returncode=1)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(master_pi.subprocess, "run", fake_run)
    monkeypatch.setattr(master_pi.time, "sleep", lambda _seconds: None)

    assert master_pi.run_fetch() is True
    assert len(commands) == 2
    assert "--retry-failed" not in commands[0]
    assert commands[1] == [*commands[0], "--retry-failed"]


def test_run_fetch_falls_back_to_full_retry_without_fresh_summary(
        monkeypatch, tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"enable_banking": {"app_id": "offline"}}))
    summary = tmp_path / ".last_fetch_summary.json"
    summary.write_text(json.dumps({
        "date": master_pi.time.strftime("%Y-%m-%d"),
        "failed_accounts": ["Old account"],
        "failed_reasons": {"Old account": "ASPSP daily limit reached"},
    }))
    monkeypatch.setattr(master_pi, "SYNC_CFG", cfg)
    monkeypatch.setattr(master_pi, "SUMMARY_FILE", summary)
    commands = []

    def fake_run(cmd, cwd):
        commands.append(cmd)
        return SimpleNamespace(returncode=1 if len(commands) == 1 else 0)

    monkeypatch.setattr(master_pi.subprocess, "run", fake_run)
    monkeypatch.setattr(master_pi.time, "sleep", lambda _seconds: None)

    assert master_pi.run_fetch() is True
    assert len(commands) == 2
    assert commands[1] == commands[0]


if __name__ == "__main__":
    test_all_quota_failures_skips_retry()
    test_mixed_session_and_quota_failures_skips_retry()
    test_unknown_failure_reason_allows_retry()
    test_missing_summary_file_allows_retry()
    print("OK — all master_pi retry-skip tests passed")
