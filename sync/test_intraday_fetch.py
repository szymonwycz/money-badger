"""Offline regressions for the intraday Enable Banking window.

requests.get is default-deny: every expected URL and parameter set must be
listed explicitly, so these tests can never fall through to the real API.
"""
import json
import sys
from datetime import date as real_date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import main as sync_main
from banks import enable_banking as eb


IBAN = "11112222333344445555666666"
FULL_IBAN = f"PL{IBAN}"
ACCOUNT_ID = "offline-account"


class MutableDate(real_date):
    current = real_date(2032, 10, 10)

    @classmethod
    def today(cls):
        return cls.current


class FakeResponse:
    def __init__(self, status_code=200, transactions=None):
        self.status_code = status_code
        self._body = {"transactions": transactions or []}
        self.text = f"offline HTTP {status_code}"

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code != 200:
            raise AssertionError(f"unexpected HTTP status {self.status_code}")


class StrictGet:
    def __init__(self, expected):
        self.expected = list(expected)
        self.calls = []

    def __call__(self, url, *, headers, params, timeout):
        if not self.expected:
            raise AssertionError(f"unexpected HTTP request: {url} params={params}")
        expected_params, response = self.expected.pop(0)
        assert url == f"{eb.EB_BASE}/accounts/{ACCOUNT_ID}/transactions"
        assert params == expected_params
        assert headers == {"X-Test": "offline"}
        assert timeout == 60
        self.calls.append(params)
        return response

    def assert_done(self):
        assert self.expected == []


def tx(tx_id, booking_date, status="BOOK"):
    row = {
        "transaction_id": tx_id,
        "booking_date": booking_date,
        "transaction_amount": {"amount": "10.00", "currency": "PLN"},
        "credit_debit_indicator": "CRDT",
        "remittance_information_unstructured": f"synthetic {tx_id}",
    }
    if status is not None:
        row["status"] = status
    return row


def make_fetcher(tmp_path, last_fetch=None):
    sync_dir = tmp_path / "sync"
    sync_dir.mkdir(exist_ok=True)
    state_path = sync_dir / ".bank_sync_state.json"
    fetcher = eb.EnableBankingFetcher(
        app_id="offline", private_key_path="unused", state_file=str(state_path),
        iban_country="PL")
    state = {"sessions": {FULL_IBAN: {
        "session_id": "offline-session", "account_id": ACCOUNT_ID,
        "valid_until": "2099-01-01T00:00:00Z",
    }}}
    if last_fetch:
        state["last_fetch"] = {FULL_IBAN: last_fetch}
    fetcher._save_state(state)
    return fetcher


@pytest.fixture(autouse=True)
def offline_clock_and_network(monkeypatch):
    MutableDate.current = real_date(2032, 10, 10)
    monkeypatch.setattr(eb, "date", MutableDate)
    monkeypatch.setattr(eb.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(eb.EnableBankingFetcher, "_headers",
                        lambda _self: {"X-Test": "offline"})
    monkeypatch.setattr(
        eb.requests, "get",
        lambda url, **kwargs: (_ for _ in ()).throw(
            AssertionError(f"unexpected HTTP request: {url} kwargs={kwargs}")))


def install_get(monkeypatch, expected):
    fake = StrictGet(expected)
    monkeypatch.setattr(eb.requests, "get", fake)
    return fake


def ids(result):
    assert len(result) == 1
    return [row["_eb_id"] for row in result[0][1]]


def test_three_intraday_passes_query_today_and_emit_each_id_once(
        monkeypatch, tmp_path):
    today = MutableDate.current.isoformat()
    yesterday = (MutableDate.current - timedelta(days=1)).isoformat()
    start = (MutableDate.current - timedelta(days=2)).isoformat()
    params = {"date_from": start, "date_to": today}
    fetcher = make_fetcher(tmp_path, yesterday)
    fake = install_get(monkeypatch, [
        (params, FakeResponse(transactions=[tx("a", today)])),
        (params, FakeResponse(transactions=[tx("a", today), tx("b", today)])),
        (params, FakeResponse(transactions=[tx("a", today), tx("b", today)])),
    ])

    assert ids(fetcher.fetch([IBAN])) == ["a"]
    fetcher.commit_fetch(FULL_IBAN)
    assert ids(fetcher.fetch([IBAN])) == ["b"]
    fetcher.commit_fetch(FULL_IBAN)
    assert ids(fetcher.fetch([IBAN])) == []
    fetcher.commit_fetch(FULL_IBAN)

    state = fetcher._load_state()
    assert state["last_fetch"][FULL_IBAN] == yesterday
    assert set(state["seen_tx_ids"][FULL_IBAN][today]) == {"a", "b"}
    fake.assert_done()


def test_gap_uses_checkpoint_until_cap_then_warns_without_failure(
        monkeypatch, tmp_path):
    old = MutableDate.current - timedelta(days=40)
    cap = (MutableDate.current - timedelta(days=30)).isoformat()
    fetcher = make_fetcher(tmp_path, old.isoformat())
    fake = install_get(monkeypatch, [
        ({"date_from": cap, "date_to": MutableDate.current.isoformat()}, FakeResponse()),
    ])

    assert ids(fetcher.fetch([IBAN], days_back=30)) == []
    assert fetcher.last_skipped == []
    warning = fetcher.window_warnings[IBAN]
    assert "were not requested" in warning
    summary = sync_main._make_fetch_summary([], [], {}, {"Offline": warning})
    assert summary["failed_accounts"] == []
    assert summary["window_warnings"] == {"Offline": warning}
    fake.assert_done()


def test_fallback_date_uses_the_same_overlap_and_cap(monkeypatch, tmp_path):
    fallback = MutableDate.current - timedelta(days=5)
    expected = (fallback + timedelta(days=1)).isoformat()
    fetcher = make_fetcher(tmp_path)
    fake = install_get(monkeypatch, [
        ({"date_from": expected, "date_to": MutableDate.current.isoformat()}, FakeResponse()),
    ])

    assert ids(fetcher.fetch(
        [IBAN], fallback_dates={FULL_IBAN: fallback.isoformat()})) == []
    assert fetcher.window_warnings == {}
    fake.assert_done()


def test_pdng_is_kept_in_raw_pull_then_emitted_when_booked(monkeypatch, tmp_path):
    day = MutableDate.current
    fetcher = make_fetcher(tmp_path, (day - timedelta(days=1)).isoformat())

    for offset, status, expected in ((0, "PDNG", []), (1, "PDNG", []), (2, "BOOK", ["p"])):
        MutableDate.current = day + timedelta(days=offset)
        params = {
            "date_from": (MutableDate.current - timedelta(days=2)).isoformat(),
            "date_to": MutableDate.current.isoformat(),
        }
        fake = install_get(monkeypatch, [
            (params, FakeResponse(transactions=[tx("p", day.isoformat(), status)])),
        ])
        assert ids(fetcher.fetch([IBAN])) == expected
        if offset == 0:
            raw_file = next((tmp_path / "raw_pulls").glob("*.json"))
            assert json.loads(raw_file.read_text())["transactions"][0]["status"] == "PDNG"
        fetcher.commit_fetch(FULL_IBAN)
        fake.assert_done()

    assert fetcher._load_state()["seen_tx_ids"][FULL_IBAN][day.isoformat()] == ["p"]


def test_missing_or_unknown_status_is_treated_as_booked(monkeypatch, tmp_path):
    today = MutableDate.current.isoformat()
    fetcher = make_fetcher(tmp_path, (MutableDate.current - timedelta(days=1)).isoformat())
    params = {"date_from": (MutableDate.current - timedelta(days=2)).isoformat(),
              "date_to": today}
    fake = install_get(monkeypatch, [
        (params, FakeResponse(transactions=[
            tx("missing", today, None), tx("unknown", today, "SOMETHING_ELSE")
        ])),
    ])
    assert ids(fetcher.fetch([IBAN])) == ["missing", "unknown"]
    fake.assert_done()


def test_commit_defensively_never_closes_today(tmp_path):
    fetcher = make_fetcher(tmp_path)
    fetcher._pending[FULL_IBAN] = {
        "last_fetch": MutableDate.current.isoformat(), "seen_ids": {}}
    fetcher.commit_fetch(FULL_IBAN)
    assert fetcher._load_state()["last_fetch"][FULL_IBAN] == (
        MutableDate.current - timedelta(days=1)).isoformat()
