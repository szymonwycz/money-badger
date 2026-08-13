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
        "failed_reasons": {"Nasze": "Connection refused (app)"},
    }))
    assert master_pi._only_non_retryable_failures() is False


def test_missing_summary_file_allows_retry():
    master_pi.SUMMARY_FILE = Path(tempfile.mkdtemp()) / "missing.json"
    assert master_pi._only_non_retryable_failures() is False


if __name__ == "__main__":
    test_all_quota_failures_skips_retry()
    test_mixed_session_and_quota_failures_skips_retry()
    test_unknown_failure_reason_allows_retry()
    test_missing_summary_file_allows_retry()
    print("OK — wszystkie testy master_pi retry-skip przeszły")
