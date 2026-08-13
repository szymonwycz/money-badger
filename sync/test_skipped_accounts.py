#!/usr/bin/env python3
"""Self-check for last_skipped — run directly, no framework/fixtures.

python3 sync/test_skipped_accounts.py

Regression coverage for the 2026-07-15 false-success bug: fetch() used to
silently `continue` past an IBAN that hit a real failure (rate limit, expired
session, missing config) without recording it anywhere. cmd_fetch() then
counted success only over the IBANs that DID come back, so a run where two
accounts got zero fetch attempts still printed "wszystkie konta przetworzone
poprawnie" and wrote the day's success marker — and the watchdog stood down
for the rest of the day while those accounts' data went stale. Now fetch()
records every such IBAN in self.last_skipped so the caller can fail the run.
"""
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent / "banks"))
sys.path.insert(0, str(Path(__file__).parent))
from banks.enable_banking import EnableBankingFetcher


def _fresh_fetcher(sessions=None):
    # state file needs a real grandparent dir — _dump_raw_pull() creates
    # raw_pulls/ next to it via Path(state_file).parent.parent.
    tmp_dir = Path(tempfile.mkdtemp()) / "sync"
    tmp_dir.mkdir()
    state_path = tmp_dir / "state.json"
    # Sessions here are keyed by full IBAN, so the fetcher needs the country
    # that config.json would give it.
    f = EnableBankingFetcher(app_id="x", private_key_path="x", state_file=str(state_path),
                             iban_country="PL")
    if sessions:
        f._save_state({"sessions": sessions})
    return f


def test_missing_session_is_recorded_as_skipped():
    f = _fresh_fetcher()  # no sessions at all
    results = f.fetch(["2222222222"], use_smart_window=False, days_back=1)
    assert results == []
    assert f.last_skipped == ["2222222222"]


def test_rate_limit_is_recorded_as_skipped_not_silently_ok():
    sessions = {"PL1111111111": {"session_id": "s", "account_id": "acc1"}}
    f = _fresh_fetcher(sessions)
    fake_429 = mock.Mock(status_code=429)
    with mock.patch("banks.enable_banking.requests.get", return_value=fake_429), \
         mock.patch.object(f, "_headers", return_value={}):
        results = f.fetch(["1111111111"], use_smart_window=False, days_back=1)
    assert results == [], "a rate-limited IBAN must not appear in results"
    assert f.last_skipped == ["1111111111"], "rate limit must be visible to the caller"


def test_last_skipped_resets_between_calls():
    sessions = {"PL2222222222": {"session_id": "s", "account_id": "acc1"}}
    f = _fresh_fetcher(sessions)
    f.last_skipped = ["stale-from-previous-run"]
    fake_ok = mock.Mock(status_code=200, json=lambda: {"transactions": []})
    with mock.patch("banks.enable_banking.requests.get", return_value=fake_ok), \
         mock.patch.object(f, "_headers", return_value={}):
        f.fetch(["2222222222"], use_smart_window=False, days_back=1)
    assert f.last_skipped == [], "a clean run must not carry over a previous run's skips"


def test_quota_exhaustion_is_remembered_across_calls():
    """Regression coverage for the 2026-07-16 rate-limit spread: a watchdog
    retry, a later hop, and the separate fetch-balances run all used to hit
    the ASPSP again for an IBAN that already 429'd earlier the same day —
    burning through the shared daily quota for the accounts that hadn't
    failed yet. Once fetch() sees a 429 for an IBAN, a second fetch() call
    the same day must skip it WITHOUT another network request."""
    sessions = {"PL1111111111": {"session_id": "s", "account_id": "acc1"}}
    f = _fresh_fetcher(sessions)
    fake_429 = mock.Mock(status_code=429)
    with mock.patch("banks.enable_banking.requests.get", return_value=fake_429) as mock_get, \
         mock.patch.object(f, "_headers", return_value={}):
        f.fetch(["1111111111"], use_smart_window=False, days_back=1)
        assert mock_get.call_count == 1
        f.fetch(["1111111111"], use_smart_window=False, days_back=1)
        assert mock_get.call_count == 1, "second call must skip without hitting the network"
    assert f.last_skipped == ["1111111111"]
    assert f.skip_reasons["1111111111"] == "ASPSP daily limit reached"


def test_quota_exhaustion_from_fetch_also_blocks_get_balances():
    """The flag is per-IBAN in the shared state file, not per-function — a
    429 seen by fetch() (transactions) must also stop get_balances() from
    querying that same IBAN again the same day."""
    sessions = {"PL1111111111": {"session_id": "s", "account_id": "acc1"}}
    f = _fresh_fetcher(sessions)
    fake_429 = mock.Mock(status_code=429)
    with mock.patch("banks.enable_banking.requests.get", return_value=fake_429), \
         mock.patch.object(f, "_headers", return_value={}):
        f.fetch(["1111111111"], use_smart_window=False, days_back=1)

    with mock.patch("banks.enable_banking.requests.get") as mock_get, \
         mock.patch.object(f, "_headers", return_value={}):
        results = f.get_balances(["1111111111"])
    mock_get.assert_not_called()
    assert results == {"1111111111": None}


def test_expired_session_401_is_also_remembered_across_calls():
    """Regression coverage for the mixed-failure gap: only 429 used to be
    remembered, so a run where one account 429'd and another hit 401 (expired
    EB session — a manual fetch-setup is needed, retry can't fix it either)
    would still re-hit EB for the 401 account on the next retry/watchdog hop.
    401 must get the exact same per-IBAN skip-for-today treatment as 429."""
    sessions = {"PL1111111111": {"session_id": "s", "account_id": "acc1"}}
    f = _fresh_fetcher(sessions)
    fake_401 = mock.Mock(status_code=401)
    with mock.patch("banks.enable_banking.requests.get", return_value=fake_401) as mock_get, \
         mock.patch.object(f, "_headers", return_value={}):
        f.fetch(["1111111111"], use_smart_window=False, days_back=1)
        assert mock_get.call_count == 1
        f.fetch(["1111111111"], use_smart_window=False, days_back=1)
        assert mock_get.call_count == 1, "second call must skip without hitting the network"
    assert f.skip_reasons["1111111111"] == "session expired (401)"

    with mock.patch("banks.enable_banking.requests.get") as mock_get2, \
         mock.patch.object(f, "_headers", return_value={}):
        results = f.get_balances(["1111111111"])
    mock_get2.assert_not_called()
    assert results == {"1111111111": None}


if __name__ == "__main__":
    test_missing_session_is_recorded_as_skipped()
    test_rate_limit_is_recorded_as_skipped_not_silently_ok()
    test_last_skipped_resets_between_calls()
    test_quota_exhaustion_is_remembered_across_calls()
    test_quota_exhaustion_from_fetch_also_blocks_get_balances()
    test_expired_session_401_is_also_remembered_across_calls()
    print("OK — all last_skipped tests passed")
