import base64
import hashlib
import json
import os
import subprocess
import tempfile
import time
from datetime import date, datetime, timedelta
from pathlib import Path
import sys
from urllib.parse import parse_qs, urlparse

import requests

sys.path.insert(0, str(Path(__file__).parent.parent))
import iban as _iban  # noqa: E402

EB_BASE = "https://api.enablebanking.com"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _sign_rs256(data: bytes, key_path: str) -> bytes:
    with tempfile.NamedTemporaryFile(delete=False) as f:
        f.write(data)
        tmp = f.name
    try:
        result = subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", key_path, tmp],
            capture_output=True,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.decode())
        return result.stdout
    finally:
        os.unlink(tmp)


def _make_jwt(app_id: str, key_path: str) -> str:
    now = int(time.time())
    header = {"alg": "RS256", "typ": "JWT", "kid": app_id}
    payload = {
        "iss": "enablebanking.com",
        "aud": "api.enablebanking.com",
        "iat": now,
        "exp": now + 3600,
    }
    h = _b64url(json.dumps(header, separators=(",", ":")).encode())
    p = _b64url(json.dumps(payload, separators=(",", ":")).encode())
    signing_input = f"{h}.{p}".encode()
    sig = _b64url(_sign_rs256(signing_input, key_path))
    return f"{h}.{p}.{sig}"


def normalize_iban(iban: str) -> str:
    return _iban.bare_iban(iban)


# Both 429 (ASPSP daily quota) and 401 (expired/revoked EB session) are dead
# ends that can't resolve within the same day — a 429 needs tomorrow's reset,
# a 401 needs a manual `fetch-setup`. Neither improves by waiting 60s and
# hitting the API again. Both get remembered per-IBAN under their own key
# ("today" is enough: worst case a same-day fix via fetch-setup just waits
# for the next natural run instead of being retried mid-run) so a watchdog
# hop, a retry, or the separate fetch-balances run skip immediately instead
# of burning another request on an account already known dead today.
_DEAD_TODAY_KEYS = {429: "quota_exhausted", 401: "session_dead"}


def _dead_today(state: dict, full_iban: str) -> str | None:
    """Returns the reason ('quota'/'session') this IBAN is already known dead
    for today, or None if it's fine to try."""
    today = date.today().isoformat()
    if state.get("quota_exhausted", {}).get(full_iban) == today:
        return "quota"
    if state.get("session_dead", {}).get(full_iban) == today:
        return "session"
    return None


def _mark_dead_today(state_file: Path, state: dict, full_iban: str, status_code: int) -> None:
    key = _DEAD_TODAY_KEYS[status_code]
    state.setdefault(key, {})[full_iban] = date.today().isoformat()
    state_file.write_text(json.dumps(state, indent=2))


RAW_PULL_RETENTION_DAYS = 90


def _dump_raw_pull(state_file: str, bare_iban: str, date_from: str, date_to: str, body: dict) -> None:
    """Zapisuje surową odpowiedź EB (przed konwersją/kategoryzacją) do raw_pulls/ —
    żeby przy debugowaniu/reprocessingu nie trzeba było znowu odpytywać EB
    (dzienny limit ASPSP na fetch w tle, patrz komentarz przy 429 w fetch())."""
    raw_dir = Path(state_file).parent.parent / "raw_pulls"
    raw_dir.mkdir(exist_ok=True)
    cutoff = time.time() - RAW_PULL_RETENTION_DAYS * 86400
    for old in raw_dir.glob("*.json"):
        try:
            if old.stat().st_mtime < cutoff:
                old.unlink()
        except OSError:
            pass
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    path = raw_dir / f"{bare_iban}_{date_from}_{date_to}_{stamp}.json"
    path.write_text(json.dumps(body, indent=2, ensure_ascii=False))


class EnableBankingFetcher:
    def __init__(self, app_id: str, private_key_path: str, state_file: str,
                 iban_country: str = ""):
        self.app_id = app_id
        self.private_key_path = private_key_path
        self.state_file = Path(state_file)
        # config.json may store IBANs with or without the country code; the API
        # always wants it. Empty means they are already stored in full.
        self.iban_country = iban_country
        # Per-IBAN state pending confirmation — see fetch()/commit_fetch().
        self._pending: dict[str, dict] = {}
        # IBANs skipped by the last fetch() due to a real failure (rate limit,
        # expired session, missing config) — NOT "already up to date". The
        # caller uses this to avoid reporting a run as successful when some
        # accounts got no fetch attempt at all (see cmd_fetch in main.py).
        self.last_skipped: list[str] = []
        # Powód pominięcia per IBAN (bare) — do czytelnych powiadomień Telegram
        # (master_pi.py), obok last_skipped które zostaje niezmienione (testy
        # w test_skipped_accounts.py sprawdzają je jako plain list[str]).
        self.skip_reasons: dict[str, str] = {}

    def _headers(self) -> dict:
        token = _make_jwt(self.app_id, self.private_key_path)
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    def _load_state(self) -> dict:
        return json.loads(self.state_file.read_text()) if self.state_file.exists() else {}

    def _save_state(self, state: dict):
        self.state_file.write_text(json.dumps(state, indent=2))

    def setup(self, ibans: list[str], aspsp_by_iban: dict | None = None,
              default_aspsp: dict | None = None):
        """One-time OAuth authorization per IBAN. Interactive: opens browser, reads redirect URL.

        aspsp_by_iban maps {full_iban: {"name": ..., "country": ...}}; default_aspsp
        covers every IBAN not listed, which is the common case of all accounts at
        one bank. Both come from config.json.
        """
        aspsp_by_iban = aspsp_by_iban or {}
        print("\n=== Enable Banking: autoryzacja kont ===\n")
        state = self._load_state()
        sessions = state.setdefault("sessions", {})

        for iban in ibans:
            full_iban = _iban.full_iban(iban, self.iban_country)
            if full_iban in sessions:
                print(f"  [{full_iban[-10:]}...] już autoryzowane, pomijam.")
                continue

            aspsp = aspsp_by_iban.get(full_iban) or default_aspsp
            if not aspsp or not aspsp.get("name"):
                print(f"  [{full_iban[-10:]}...] pomijam: brak banku w config.json "
                      f"(ustaw enable_banking.aspsp_names lub enable_banking.default_aspsp).")
                continue
            print(f"\nAutoryzacja konta: {full_iban} ({aspsp['name']})")
            body = {
                "access": {
                    "valid_until": (datetime.utcnow() + timedelta(days=180)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                },
                "aspsp": aspsp,
                "state": full_iban,
                "redirect_url": "https://localhost/",
                "psu_type": "personal",
            }
            r = requests.post(f"{EB_BASE}/auth", headers=self._headers(), json=body, timeout=30)
            r.raise_for_status()
            auth_url = r.json().get("url")

            print(f"\nOtwórz link i zaloguj się do {aspsp['name']}:\n\n  {auth_url}\n")
            print("Po autoryzacji przeglądarka przekieruje na https://localhost/ (błąd jest OK).")
            print("Skopiuj PEŁNY URL z paska przeglądarki i wklej poniżej:")
            redirect_url = input("URL: ").strip()

            parsed = urlparse(redirect_url)
            params = parse_qs(parsed.query)
            auth_code = params.get("code", [None])[0]
            if not auth_code:
                print(f"  Brak parametru 'code' w URL — pomijam.")
                continue

            r2 = requests.post(
                f"{EB_BASE}/sessions",
                headers=self._headers(),
                json={"code": auth_code},
                timeout=30,
            )
            r2.raise_for_status()
            session_id = r2.json().get("session_id") or r2.json().get("id")

            r3 = requests.get(f"{EB_BASE}/sessions/{session_id}", headers=self._headers(), timeout=30)
            r3.raise_for_status()
            session_info = r3.json()

            account_id = None
            for acc in session_info.get("accounts", []):
                if isinstance(acc, str):
                    account_id = acc
                    break
                acc_iban = (acc.get("account_id") or {}).get("iban") or acc.get("iban", "")
                if acc_iban == full_iban:
                    account_id = acc.get("uid") or acc.get("id")
                    break
            if not account_id:
                first = (session_info.get("accounts") or [None])[0]
                if first:
                    account_id = first if isinstance(first, str) else (first.get("uid") or first.get("id"))

            sessions[full_iban] = {"session_id": session_id, "account_id": account_id}
            print(f"  OK. session_id: {session_id}, account_id: {account_id}")

        self._save_state(state)
        print("\nGotowe! Stan zapisany.")

    def get_balances(self, ibans: list[str]) -> dict[str, float | None]:
        """Fetch current balance per IBAN. Returns {bare_iban: amount or None on failure}."""
        state    = self._load_state()
        sessions = state.get("sessions", {})
        results: dict[str, float | None] = {}

        preferred_types = ("interimAvailable", "expected", "closingBooked", "interimBooked")

        for iban in ibans:
            full_iban = _iban.full_iban(iban, self.iban_country)
            bare_iban = normalize_iban(iban)

            session_info = sessions.get(full_iban)
            account_id   = session_info.get("account_id") if session_info else None
            if not account_id:
                print(f"  [{bare_iban[-10:]}...] brak sesji — uruchom: python main.py fetch-setup")
                results[bare_iban] = None
                continue

            dead_reason = _dead_today(state, full_iban)
            if dead_reason == "quota":
                print(f"  [{bare_iban[-10:]}...] limit ASPSP już dziś wyczerpany — pomijam bez zapytania")
                results[bare_iban] = None
                continue
            if dead_reason == "session":
                print(f"  [{bare_iban[-10:]}...] sesja już dziś martwa — pomijam bez zapytania (fetch-setup)")
                results[bare_iban] = None
                continue

            r = requests.get(
                f"{EB_BASE}/accounts/{account_id}/balances",
                headers=self._headers(),
                timeout=30,
            )
            if r.status_code == 429:
                # ASPSP-side daily quota for background fetches (zwykle ~4x/dzień,
                # bez obecności PSU) — nie throttling per-sekundę. Krótki retry nic
                # nie da, EB każe czekać ~6h / do jutra. https://enablebanking.com/docs/faq/
                print(f"  [{bare_iban[-10:]}...] limit ASPSP wyczerpany na dziś — spróbuj za kilka godzin/jutro")
                _mark_dead_today(self.state_file, state, full_iban, 429)
                results[bare_iban] = None
                continue
            if r.status_code == 401:
                print(f"  [{bare_iban[-10:]}...] sesja wygasła — uruchom: python main.py fetch-setup")
                _mark_dead_today(self.state_file, state, full_iban, 401)
                results[bare_iban] = None
                continue
            if r.status_code != 200:
                print(f"  [{bare_iban[-10:]}...] błąd pobierania salda ({r.status_code})")
                results[bare_iban] = None
                continue

            balances = r.json().get("balances", [])
            chosen = None
            for btype in preferred_types:
                chosen = next((b for b in balances if b.get("balance_type") == btype), None)
                if chosen:
                    break
            if not chosen and balances:
                chosen = balances[0]

            if not chosen:
                results[bare_iban] = None
                continue

            amount = (chosen.get("balance_amount") or {}).get("amount")
            results[bare_iban] = float(amount) if amount is not None else None
            time.sleep(1)

        return results

    def fetch(self, ibans: list[str], days_back: int = 30, use_smart_window: bool = True,
              fallback_dates: dict | None = None, dry_run: bool = False) -> list[tuple[str, list[dict]]]:
        """Fetch transactions. Returns list of (iban_without_PL, [tx_dict]).

        Smart window (use_smart_window=True): per-IBAN date_from = last_fetch + 1 day,
        capped at days_back (days_back is a MAX catch-up window, not the normal
        increment — in steady state only 1 day is fetched per run).
        Forced mode (use_smart_window=False): always fetches last days_back days.

        fallback_dates: {full_iban: "YYYY-MM-DD"} — used when there is no local
        last_fetch state for an IBAN (e.g. state file lost/reset). Instead of
        blindly capping to days_back, resume from the last known transaction
        date for that account (still capped at days_back as a floor).

        IMPORTANT: this method no longer advances the last_fetch checkpoint or
        persists dedup IDs by itself — it only stages them in self._pending.
        The caller MUST call commit_fetch(full_iban) once it has confirmed the
        returned transactions were actually categorized and written out.
        Advancing the checkpoint any earlier (the old behavior) meant a crash
        anywhere downstream — Anthropic API, disk, the app — silently and
        permanently lost that day's transactions, because a retry would see
        "already up to date" and skip straight past them. This bit us for real
        on 2026-07-13 (Anthropic billing) and again on 2026-07-12 (unrelated
        cause) — see raw/2026-07-13-router-b535-awaria-i-money-badger-diagnoza-salda.md.
        """
        state = self._load_state()
        sessions = state.get("sessions", {})
        last_fetches = state.get("last_fetch", {})
        seen_by_date = state.get("seen_tx_ids", {})  # {full_iban: {"YYYY-MM-DD": [ids]}}
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        cap_date = (date.today() - timedelta(days=days_back)).isoformat()
        fallback_dates = fallback_dates or {}
        results = []
        self.last_skipped = []
        self.skip_reasons = {}

        for iban in ibans:
            full_iban = _iban.full_iban(iban, self.iban_country)
            bare_iban = normalize_iban(iban)

            session_info = sessions.get(full_iban)
            if not session_info:
                print(f"  [{bare_iban[-10:]}...] brak sesji — uruchom: python main.py fetch-setup")
                self.last_skipped.append(bare_iban)
                self.skip_reasons[bare_iban] = "no EB session (run fetch-setup)"
                continue

            account_id = session_info.get("account_id")
            if not account_id:
                print(f"  [{bare_iban[-10:]}...] brak account_id — uruchom ponownie fetch-setup")
                self.last_skipped.append(bare_iban)
                self.skip_reasons[bare_iban] = "no account_id (run fetch-setup)"
                continue

            dead_reason = _dead_today(state, full_iban)
            if dead_reason == "quota":
                print(f"  [{bare_iban[-10:]}...] limit ASPSP już dziś wyczerpany — pomijam bez zapytania")
                self.last_skipped.append(bare_iban)
                self.skip_reasons[bare_iban] = "ASPSP daily limit reached"
                continue
            if dead_reason == "session":
                print(f"  [{bare_iban[-10:]}...] sesja już dziś martwa — pomijam bez zapytania (fetch-setup)")
                self.last_skipped.append(bare_iban)
                self.skip_reasons[bare_iban] = "session expired (401)"
                continue

            # Determine date_from per IBAN
            if use_smart_window and full_iban in last_fetches:
                next_day = (date.fromisoformat(last_fetches[full_iban]) + timedelta(days=1)).isoformat()
                date_from = max(next_day, cap_date)
            elif use_smart_window and full_iban in fallback_dates:
                next_day = (date.fromisoformat(fallback_dates[full_iban]) + timedelta(days=1)).isoformat()
                date_from = max(next_day, cap_date)
                print(f"  [{bare_iban[-10:]}...] brak lokalnego stanu — wznawiam od ostatniej transakcji w aplikacji ({fallback_dates[full_iban]})")
            else:
                date_from = cap_date

            if date_from > yesterday:
                print(f"  [{bare_iban[-10:]}...] już aktualne (last_fetch={last_fetches.get(full_iban, '—')}), pomijam")
                continue

            print(f"  Pobieranie {bare_iban[-10:]}... (od {date_from} do {yesterday})")
            params = {"date_from": date_from, "date_to": yesterday}
            r = requests.get(
                f"{EB_BASE}/accounts/{account_id}/transactions",
                headers=self._headers(),
                params=params,
                timeout=60,
            )

            if r.status_code == 429:
                # Patrz komentarz w get_balances() — dzienny limit ASPSP, nie
                # throttling. Retry po 60s tylko marnuje kolejne wywołanie z tej
                # samej puli; pomijamy to konto i próbujemy resztę.
                print(f"  [{bare_iban[-10:]}...] limit ASPSP wyczerpany na dziś — spróbuj za kilka godzin/jutro")
                _mark_dead_today(self.state_file, state, full_iban, 429)
                self.last_skipped.append(bare_iban)
                self.skip_reasons[bare_iban] = "ASPSP daily limit reached"
                continue

            if r.status_code == 401:
                print(f"  Sesja wygasła — uruchom: python main.py fetch-setup (konto {bare_iban[-10:]}...)")
                _mark_dead_today(self.state_file, state, full_iban, 401)
                self.last_skipped.append(bare_iban)
                self.skip_reasons[bare_iban] = "session expired (401)"
                continue

            r.raise_for_status()
            body = r.json()
            raw_txs = body.get("transactions", [])
            _dump_raw_pull(self.state_file, bare_iban, date_from, yesterday, body)
            print(f"  → {len(raw_txs)} transakcji")

            converted = [self._convert_tx(tx, bare_iban) for tx in raw_txs]

            # Deduplicate against previously *committed* seen IDs only (not
            # against anything still pending from an earlier, uncommitted
            # attempt) — otherwise a retry after a downstream crash would see
            # its own not-yet-pushed transactions as "duplicates" and drop them.
            iban_seen = dict(seen_by_date.get(full_iban, {}))
            all_seen = {ebid for ids in iban_seen.values() for ebid in ids}
            before = len(converted)
            converted = [tx for tx in converted if tx["_eb_id"] not in all_seen]
            if len(converted) < before:
                print(f"  ⚠ pominięto {before - len(converted)} duplikat(ów)")

            # Record new IDs grouped by booking_date; prune entries older than days_back
            for tx in converted:
                d = tx["booking_date"]
                iban_seen.setdefault(d, []).append(tx["_eb_id"])
            cutoff = (date.today() - timedelta(days=days_back)).isoformat()
            iban_seen = {d: ids for d, ids in iban_seen.items() if d >= cutoff}

            results.append((bare_iban, converted))

            # Staged, not saved — see commit_fetch().
            self._pending[full_iban] = {"last_fetch": yesterday, "seen_ids": iban_seen}

            time.sleep(2)

        return results

    def commit_fetch(self, full_iban: str):
        """Confirm that full_iban's transactions from the last fetch() call were
        safely categorized and written out — only now is it safe to advance the
        checkpoint. No-op if that IBAN has nothing pending (e.g. fetch() skipped
        it, or it was already committed)."""
        pending = self._pending.pop(full_iban, None)
        if not pending:
            return
        state = self._load_state()
        state.setdefault("last_fetch", {})[full_iban] = pending["last_fetch"]
        state.setdefault("seen_tx_ids", {})[full_iban] = pending["seen_ids"]
        self._save_state(state)

    def _convert_tx(self, tx: dict, account_iban: str) -> dict:
        """Convert EB transaction dict → the internal transaction format."""
        amount_raw = tx.get("transaction_amount") or {}
        amount = float(amount_raw.get("amount", 0) if isinstance(amount_raw, dict) else amount_raw or 0)

        indicator = tx.get("credit_debit_indicator", "DBIT")
        amount = -abs(amount) if indicator == "DBIT" else abs(amount)

        booking_date = tx.get("booking_date") or tx.get("value_date") or date.today().isoformat()
        transaction_date = tx.get("value_date") or booking_date

        title = (
            tx.get("remittance_information_unstructured")
            or (tx.get("remittance_information_unstructured_array") or [""])[0]
            # Revolut's connector doesn't use the "_unstructured" keys at all —
            # it puts the same kind of text under plain "remittance_information",
            # and never fills creditor/debtor name either, so without this
            # fallback these transactions land with a fully blank description.
            or (tx.get("remittance_information") or [""])[0]
            or ""
        )
        if isinstance(title, list):
            title = " ".join(title)

        if amount < 0:
            counterpart = tx.get("creditor_name") or (tx.get("creditor") or {}).get("name") or ""
            c_iban = (
                (tx.get("creditor_account") or {}).get("iban")
                or ((tx.get("creditor") or {}).get("account") or {}).get("iban")
                or ""
            )
        else:
            counterpart = tx.get("debtor_name") or (tx.get("debtor") or {}).get("name") or ""
            c_iban = (
                (tx.get("debtor_account") or {}).get("iban")
                or ((tx.get("debtor") or {}).get("account") or {}).get("iban")
                or ""
            )

        eb_id = (
            tx.get("transaction_id")
            or tx.get("id")
            or tx.get("entry_reference")
            or tx.get("internal_transaction_id")
        )
        if not eb_id:
            eb_id = hashlib.sha1(
                f"{booking_date}|{amount}|{title.strip()[:80]}|{normalize_iban(c_iban)}".encode()
            ).hexdigest()[:16]

        return {
            "booking_date":     booking_date,
            "transaction_date": transaction_date,
            "title":            title.strip(),
            "counterpart":      counterpart.strip(),
            "counterpart_iban": normalize_iban(c_iban),
            "amount":           amount,
            "account_iban":     normalize_iban(account_iban),
            "_eb_id":           eb_id,
        }
