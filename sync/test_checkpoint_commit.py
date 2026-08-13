#!/usr/bin/env python3
"""Self-check for EnableBankingFetcher's staged-checkpoint mechanism — run directly,
no framework/fixtures.

python3 sync/test_checkpoint_commit.py

Regression coverage for the 2026-07-13/07-12 data-loss bug: fetch() used to advance
last_fetch (and persist dedup IDs) immediately after downloading from EB, before the
caller had categorized+written anything. A crash downstream (Anthropic billing, disk,
the app) then permanently lost that day's transactions, because a retry saw
"already up to date" and skipped straight past them. Now fetch() only stages the
checkpoint in memory; only commit_fetch() persists it, and cmd_fetch() only calls
that after a successful write.
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "banks"))
sys.path.insert(0, str(Path(__file__).parent))
from banks.enable_banking import EnableBankingFetcher


def _fresh_fetcher():
    tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    tmp.close()
    os.unlink(tmp.name)  # state file must not exist yet — matches a real fresh account
    return EnableBankingFetcher(app_id="x", private_key_path="x", state_file=tmp.name)


def test_uncommitted_pending_does_not_touch_state():
    f = _fresh_fetcher()
    f._pending["PL123"] = {"last_fetch": "2026-07-13", "seen_ids": {"2026-07-13": ["a", "b"]}}

    state = f._load_state()
    assert state.get("last_fetch", {}).get("PL123") is None, "checkpoint must not exist before commit"


def test_commit_persists_only_that_iban():
    f = _fresh_fetcher()
    f._pending["PL123"] = {"last_fetch": "2026-07-13", "seen_ids": {"2026-07-13": ["a", "b"]}}
    f._pending["PL456"] = {"last_fetch": "2026-07-13", "seen_ids": {"2026-07-13": ["c"]}}

    f.commit_fetch("PL123")

    state = f._load_state()
    assert state["last_fetch"]["PL123"] == "2026-07-13"
    assert "PL456" not in state.get("last_fetch", {}), "uncommitted IBAN must stay uncommitted"
    assert state["seen_tx_ids"]["PL123"] == {"2026-07-13": ["a", "b"]}
    assert "PL123" not in f._pending, "committed entry should be cleared from pending"
    assert "PL456" in f._pending, "the other IBAN's pending entry must survive"


def test_commit_with_nothing_pending_is_a_safe_noop():
    f = _fresh_fetcher()
    f.commit_fetch("PLdoesnotexist")  # must not raise
    state = f._load_state()
    assert state == {} or "last_fetch" not in state or "PLdoesnotexist" not in state["last_fetch"]


def test_double_commit_is_a_safe_noop():
    f = _fresh_fetcher()
    f._pending["PL123"] = {"last_fetch": "2026-07-13", "seen_ids": {}}
    f.commit_fetch("PL123")
    f.commit_fetch("PL123")  # second call: nothing pending anymore, must not raise or corrupt state
    state = f._load_state()
    assert state["last_fetch"]["PL123"] == "2026-07-13"


if __name__ == "__main__":
    test_uncommitted_pending_does_not_touch_state()
    test_commit_persists_only_that_iban()
    test_commit_with_nothing_pending_is_a_safe_noop()
    test_double_commit_is_a_safe_noop()
    print("OK — wszystkie testy checkpoint commit przeszły")
