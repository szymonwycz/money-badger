#!/usr/bin/env python3
"""
money-badger sync – bank transactions → categorization → budget app

Commands:
    python main.py setup            # first-run configuration wizard
    python main.py fetch-setup      # one-time bank authorization (Enable Banking)
    python main.py fetch            # pull transactions, categorize, write CSV
    python main.py fetch --dry-run
    python main.py fetch-balances   # pull account balances, push to the app
    python main.py learn            # update rules.json from corrections made in the app
    python main.py learn --dry-run
"""

import argparse
import csv
import json
import os
import shutil
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import env_file

env_file.load()

import iban as _iban

from logging_setup import start_logging
import budget_client
from budget_client import BUDGET_URL

import llm

# ─── Config paths ─────────────────────────────────────────────────────────────

BASE_DIR     = Path(__file__).parent
PROJECT_ROOT = BASE_DIR.parent
ENV_FILE     = BASE_DIR / ".env"
CFG_FILE     = BASE_DIR / "config.json"
RULES_FILE   = BASE_DIR / "rules.json"


def resolve_path(p: str) -> Path:
    """Config paths may be relative to the project root, so config.json survives
    being copied to another machine with a different install directory."""
    path = Path(p).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path

# ─── Categories ───────────────────────────────────────────────────────────────
#
# The category tree belongs to the budget app, not to this script. Reading it live
# means the LLM prompt always names categories that actually exist — a hardcoded
# copy drifts, and every drifted name comes back as an invented category that
# validate_category() has to throw away.

_categories = None  # cache: (prompt_text, valid_names) — see load_categories()


def load_categories(force: bool = False) -> tuple:
    """Fetch the live category tree from the app. Cached for the process lifetime."""
    global _categories
    if _categories is not None and not force:
        return _categories

    expenses = budget_client.get("/api/categories")
    incomes = budget_client.get("/api/income-categories")

    lines, valid = ["EXPENSES:"], set()
    for group in expenses:
        children = [c["name"] for c in group.get("children", [])]
        lines.append(f"  {group['name']}" + (f": {' | '.join(children)}" if children else ""))
        valid.add(group["name"])
        valid.update(f"{group['name']}: {c}" for c in children)

    income_names = [c["name"] for c in incomes]
    lines += ["", "INCOME:", "  " + " | ".join(income_names)]
    valid.update(income_names)

    _categories = ("\n".join(lines), valid)
    return _categories


def valid_categories() -> set:
    return load_categories()[1]


# ─── Config loading ────────────────────────────────────────────────────────────

def load_env() -> dict:
    """Load .env file as a dict (key=value lines, ignores comments)."""
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def load_config() -> dict:
    if CFG_FILE.exists():
        return json.loads(CFG_FILE.read_text(encoding="utf-8"))
    return {}


def get_api_key(env: dict) -> str:
    return (
        env.get("ANTHROPIC_API_KEY")
        or os.environ.get("ANTHROPIC_API_KEY")
        or ""
    )


def check_config_ready() -> tuple[bool, list[str]]:
    """Return (ok, list_of_missing_items)."""
    missing = []
    env = load_env()
    if not get_api_key(env):
        missing.append("LLM nieskonfigurowany (LLM_PROVIDER / ANTHROPIC_API_KEY) — kategoryzacja tylko regułami")
    cfg = load_config()
    for key in ("output_dir", "account_names"):
        if key not in cfg:
            missing.append(f"config.json: brakuje pola '{key}'")
    return len(missing) == 0, missing

# ─── Setup wizard ──────────────────────────────────────────────────────────────

def _ask(prompt: str, default: str = "") -> str:
    hint = f" [{default}]" if default else ""
    val = input(f"{prompt}{hint}: ").strip()
    return val or default


def run_setup():
    print("\n=== money-badger sync: konfiguracja ===\n")
    print("Podaj puste pole (Enter) gdy chcesz użyć domyślnej wartości.\n")

    env  = load_env()
    cfg  = load_config()

    # ── API key ──────────────────────────────────────────────
    current_key = get_api_key(env)
    masked = f"sk-ant-...{current_key[-6:]}" if current_key else ""
    print("── Klucz Anthropic API (Enter = bez LLM, same reguły) ─")
    new_key = _ask("ANTHROPIC_API_KEY", masked)
    if new_key and not new_key.startswith("sk-ant-..."):
        env["ANTHROPIC_API_KEY"] = new_key

    # ── Ścieżki ───────────────────────────────────────────────
    print("\n── Ścieżki folderów ──────────────────────────────────")
    cfg["output_dir"]     = _ask("Folder na CSV z transakcjami (Export)", cfg.get("output_dir", "Export"))
    cfg["corrections_dir"] = _ask("Folder na poprawki z aplikacji",       cfg.get("corrections_dir", "Corrections"))

    # ── Konta własne ──────────────────────────────────────────
    print("\n── Konta bankowe (IBAN → nazwa konta w aplikacji) ────")
    print("Wpisuj IBAN i nazwę po kolei. Enter bez IBAN = koniec.\n")

    account_names: dict = cfg.get("account_names", {})
    own_ibans: list     = cfg.get("own_ibans", list(account_names.keys()))
    savings_ibans: list = cfg.get("savings_ibans", [])

    while True:
        iban = _iban.bare_iban(input("  IBAN (lub Enter = koniec): "))
        if not iban:
            break
        name = _ask(f"  Nazwa konta dla {iban[-8:]}", account_names.get(iban, ""))
        account_names[iban] = name
        if iban not in own_ibans:
            is_own = _ask(f"  Konto własne? (t/n)", "t").lower()
            if is_own in ("t", "tak", "y", "yes"):
                own_ibans.append(iban)

    cfg["account_names"] = account_names
    cfg["own_ibans"]     = own_ibans

    # ── Konta oszczędnościowe ─────────────────────────────────
    print("\n── Konta oszczędnościowe (przelewy z nich = 'From assets') ──")
    print("Wpisuj IBANy. Enter bez IBAN = koniec.\n")
    while True:
        iban = _iban.bare_iban(input("  IBAN oszczędnościowy (lub Enter = koniec): "))
        if not iban:
            break
        if iban not in savings_ibans:
            savings_ibans.append(iban)

    cfg["savings_ibans"] = savings_ibans
    cfg["batch_size"]    = cfg.get("batch_size", 30)

    # ── Zapis ─────────────────────────────────────────────────
    print("\n── Zapisuję konfigurację ─────────────────────────────")

    env_lines = [f"{k}={v}" for k, v in env.items()]
    ENV_FILE.write_text("\n".join(env_lines) + "\n", encoding="utf-8")
    print(f"  .env → {ENV_FILE}")

    CFG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  config.json → {CFG_FILE}")

    print("\nGotowe! Uruchom teraz: python main.py fetch-setup\n")

# ─── Filtering ─────────────────────────────────────────────────────────────────

# Banks describe the same event in their own words, so every phrase here is
# configurable — see "filters" in config.json (config.example.json documents it).
DEFAULT_FILTERS = {
    "own_transfer_titles": ["between your own accounts"],
    "transfer_to_account": [],
    "savings_withdrawal_titles": [],
    "skip_incoming_titles": [],
}


def filter_transactions(transactions: list, own_ibans: list, savings_ibans: list,
                        filters: dict = None) -> list:
    f = {**DEFAULT_FILTERS, **(filters or {})}
    own_set     = set(own_ibans)
    savings_set = set(savings_ibans)
    filtered    = []

    for tx in transactions:
        if tx["amount"] == 0:
            continue

        c_iban      = tx["counterpart_iban"]
        title_lower = tx["title"].lower()
        text_lower  = f"{title_lower} {tx['counterpart'].lower()}"
        is_own_title = any(t in title_lower for t in f["own_transfer_titles"])

        # A rule matches when every one of its phrases appears in the text, which
        # is how "card payment" + "revolut" together mean a top-up but neither does alone.
        to_account = next((r["account"] for r in f["transfer_to_account"]
                           if all(m.lower() in text_lower for m in r["match"])), None)

        if to_account is not None:
            tx["_transfer"]   = True
            tx["_savings"]    = False
            tx["_to_account"] = to_account
        elif c_iban in savings_set or any(t in title_lower for t in f["savings_withdrawal_titles"]):
            tx["_transfer"]   = False
            tx["_savings"]    = True
            tx["_to_account"] = ""
        elif tx["amount"] > 0 and any(t in title_lower for t in f["skip_incoming_titles"]):
            # Incoming leg of a card top-up (Apple Pay -> Revolut). Neither leg
            # carries an IBAN, so the own_set test below never sees it. The funding
            # account books its own outgoing transfer a day or two later
            # and that row is the whole transfer — keeping this side too invents
            # income and credits the balance twice.
            print(f"  Pomijam przychodzące doładowanie (druga noga przelewu): {tx['title'][:60]}")
            continue
        elif c_iban in own_set or is_own_title:
            if tx["amount"] > 0:
                continue  # skip incoming side — the outgoing entry covers the full transfer
            tx["_transfer"]   = True
            tx["_savings"]    = False
            tx["_to_account"] = ""
        else:
            tx["_transfer"]   = False
            tx["_savings"]    = False
            tx["_to_account"] = ""

        filtered.append(tx)

    return filtered

# ─── Rules ────────────────────────────────────────────────────────────────────

def load_rules() -> dict:
    if RULES_FILE.exists():
        return json.loads(RULES_FILE.read_text(encoding="utf-8"))
    return {"keywords": {}, "patterns": [], "merchant_map": {}}


def save_rules(rules: dict):
    RULES_FILE.write_text(json.dumps(rules, ensure_ascii=False, indent=2), encoding="utf-8")


def apply_rules(title: str, counterpart: str, amount: float, rules: dict) -> str | None:
    if amount > 0:
        return None

    # .strip(): counterpart is often empty, which left a trailing space, which let
    # keywords ending in a space match at the end of the title — "bp " (BP station)
    # matched "exchanged to gbp " and filed every Revolut FX under Car: Fuel.
    text = f"{title} {counterpart}".lower().strip()

    for p in rules.get("patterns", []):
        if p["pattern"].lower() in text:
            return p["category"]

    for merchant, category in rules.get("merchant_map", {}).items():
        if merchant.lower() in text:
            return category

    for keyword, category in rules.get("keywords", {}).items():
        if keyword.lower() in text:
            return category

    return None

# ─── Categorization ───────────────────────────────────────────────────────────

def validate_category(category: str) -> str:
    if category == "":
        return ""
    return category if category in valid_categories() else "CHECK ME"


def _categorize_batch(transactions: list, api_key: str, hints: list = None) -> list:
    tx_list = [
        {
            "idx":         i,
            "amount":      tx["amount"],
            "date":        tx["transaction_date"],
            "title":       tx["title"],
            "counterpart": tx["counterpart"],
            "is_transfer": tx.get("_transfer", False),
        }
        for i, tx in enumerate(transactions)
    ]

    # Local knowledge the category names alone don't convey — which merchants are
    # really petrol stations, what an unrecognizable acronym is. From config.json.
    hint_lines = "".join(f"\n- {h}" for h in (hints or []))

    prompt = f"""Jesteś asystentem kategoryzującym transakcje bankowe.

Oto dostępne kategorie i podkategorie:
{load_categories()[0]}

Zasady:
- Kwoty ujemne (amount < 0) to wydatki → kategorie z EXPENSES
- Kwoty dodatnie (amount > 0) to przychody → kategorie z INCOME
- is_transfer=true → kategoria "" (Money Transfer, kategoria pusta)
- Separator kategorii i podkategorii: ": " (np. "Transport: Fuel")
- Jeśli nie jesteś pewny → użyj "CHECK ME"
- NIE twórz nowych kategorii{hint_lines}

Transakcje:
{json.dumps(tx_list, ensure_ascii=False, indent=2)}

Odpowiedz WYŁĄCZNIE w formacie JSON:
[
  {{"idx": 0, "category": "Car: Fuel", "note": "krótka notatka"}},
  ...
]"""

    raw = llm.complete(prompt, task="categorize", max_tokens=4000, api_key=api_key)
    cat_map = {item["idx"]: item for item in llm.extract_json(raw)}

    return [
        {**tx, "category": cat_map.get(i, {}).get("category", "CHECK ME"),
               "note":     cat_map.get(i, {}).get("note", "")}
        for i, tx in enumerate(transactions)
    ]


def categorize_transactions(transactions: list, cfg: dict, api_key: str) -> list:
    if not transactions:
        return []

    rules      = load_rules()
    own_ibans  = cfg["own_ibans"]
    batch_size = cfg.get("batch_size", 30)

    pre_categorized: dict[int, dict] = {}

    for i, tx in enumerate(transactions):
        if tx.get("_transfer"):
            pre_categorized[i] = {"category": "", "note": ""}
        elif tx.get("_savings"):
            pre_categorized[i] = {"category": "From assets", "note": ""}

    for i, tx in enumerate(transactions):
        if i in pre_categorized:
            continue
        cat = apply_rules(tx["title"], tx["counterpart"], tx["amount"], rules)
        if cat:
            pre_categorized[i] = {"category": cat, "note": ""}

    rules_count = len(pre_categorized)
    print(f"  Rules/transfers: {rules_count}/{len(transactions)} bez LLM")

    unknown_pairs   = [(i, tx) for i, tx in enumerate(transactions) if i not in pre_categorized]
    unknown_indices = [i for i, _ in unknown_pairs]
    unknown_txs     = [tx for _, tx in unknown_pairs]

    llm_results = []
    if unknown_txs and not (llm.configured() or api_key):
        print(f"  Bez LLM: {len(unknown_txs)} transakcji trafia do CHECK ME.")
    elif unknown_txs:
        total = (len(unknown_txs) - 1) // batch_size + 1
        for j in range(0, len(unknown_txs), batch_size):
            part = j // batch_size + 1
            batch = unknown_txs[j:j + batch_size]
            print(f"  LLM partia {part}/{total} ({len(batch)} transakcji)...")
            llm_results.extend(_categorize_batch(batch, api_key, cfg.get("prompt_hints")))

    llm_map = {unknown_indices[i]: llm_results[i] for i in range(len(llm_results))}

    result = []
    for i, tx in enumerate(transactions):
        if i in pre_categorized:
            entry = pre_categorized[i]
        else:
            entry = llm_map.get(i, {"category": "CHECK ME", "note": ""})
        cat = validate_category(entry.get("category", ""))
        result.append({**tx, "category": cat, "note": entry.get("note", "")})

    return result

# ─── CSV generation ───────────────────────────────────────────────────────────
#
# The column layout and the "moneypro_" filename prefix are what push_actuals.py
# reads; keep them in step if you ever change either.

def generate_money_pro_csv(transactions: list, account_name: str, account_names: dict) -> str:
    lines = ["Date;Amount;Account;Amount received;Account (to);Balance;Category;Description;Transaction Type;Agent;Check #;Class;"]

    for tx in transactions:
        amount  = tx["amount"]
        amt_abs = abs(amount)
        amt_str = f"{amt_abs:,.2f}".replace(",", " ").replace(".", ",") + " zł"

        if tx.get("_transfer"):
            tx_type     = "Money Transfer"
            category    = ""
            account_to  = tx.get("_to_account") or account_names.get(tx.get("counterpart_iban", ""), "")
            if not account_to:
                print(f"  UWAGA: przelew własny bez nazwy konta docelowego — IBAN {tx.get('counterpart_iban', '?')[-10:]}... Dodaj go do account_names w config.json")
            amount_recv = amt_str
        elif amount > 0:
            tx_type     = "Income"
            category    = tx.get("category", "")
            account_to  = ""
            amount_recv = ""
        else:
            tx_type     = "Expense"
            category    = tx.get("category", "")
            account_to  = ""
            amount_recv = ""

        dt = tx["transaction_date"]
        try:
            dt_fmt = f"{dt[8:10]}/{dt[5:7]}/{dt[0:4]}, 01:00"
        except Exception:
            dt_fmt = dt

        note      = tx.get("note", "")
        # card/POS purchases (Allegro, Action, ...) often leave "title" (remittance info)
        # blank — banks only populate it for transfers. Merchant name lives in
        # "counterpart" instead; fall back to it so the description isn't empty.
        desc      = (tx["title"] or tx["counterpart"])[:255]
        full_desc = f"{desc} [{note}]" if note else desc

        lines.append(
            f"{dt_fmt};{amt_str};{account_name};{amount_recv};{account_to};;"
            f"{category};{full_desc};{tx_type};;;;"
        )

    return "\n".join(lines)

# ─── Splitting bundled payments ───────────────────────────────────────────────
#
# One outgoing transfer sometimes covers several things billed together — a loan
# instalment plus an overpayment plus a furniture instalment, all in one bank row.
# Categorizing that as a single expense hides the parts, so a split rule breaks it
# back apart. Rules live in config.json ("split_rules"); see config.example.json.

def split_transactions(transactions: list, rules: list = None) -> list:
    """Expand transactions matching a split rule into their component expenses.

    A rule matches on the counterpart IBAN plus a total range — the range is the
    sanity check that stops an unrelated payment to the same account from being
    carved up. Exactly one part carries "remainder" as its amount and absorbs
    whatever the fixed parts leave over.
    """
    if not rules:
        return list(transactions)

    result = []
    for tx in transactions:
        amount = tx["amount"]
        iban = (tx.get("counterpart_iban") or "").replace(" ", "")
        rule = next((r for r in rules
                     if amount < 0
                     and iban == r["counterpart_iban"].replace(" ", "")
                     and r["total_range"][0] <= abs(amount) <= r["total_range"][1]), None)
        if rule is None:
            result.append(tx)
            continue

        fixed = sum(p["amount"] for p in rule["parts"] if p["amount"] != "remainder")
        remainder = amount + fixed  # amount is negative; fixed parts are positive magnitudes
        print(f"  Rozbijam przelew {amount:.2f} na {len(rule['parts'])} pozycji")

        base = {k: v for k, v in tx.items()
                if k not in ("amount", "category", "_transfer", "_to_account")}
        for part in rule["parts"]:
            row = dict(base)
            row["amount"] = remainder if part["amount"] == "remainder" else -part["amount"]
            row["category"] = part["category"]
            row["_transfer"] = False
            row["_to_account"] = ""
            row["title"] = f"{tx['title']} — {part.get('label', part['category'])}"
            result.append(row)

    return result

# ─── fetch command ────────────────────────────────────────────────────────────

def _iban_country(cfg: dict) -> str:
    """Country code for IBANs stored without one. Taken from the default bank,
    since an account's IBAN and its bank are in the same country in practice."""
    eb = cfg.get("enable_banking", {})
    return (eb.get("iban_country")
            or (eb.get("default_aspsp") or {}).get("country", "")
            or "")


def _eb_config(cfg: dict) -> dict:
    """Return enable_banking sub-config, raising if missing."""
    eb = cfg.get("enable_banking")
    if not eb:
        print("Brak sekcji 'enable_banking' w config.json.")
        print("Dodaj ją lub uruchom: python main.py fetch-setup")
        sys.exit(1)
    return eb


def cmd_fetch_setup(args):
    cfg = load_config()
    eb = _eb_config(cfg)

    from banks.enable_banking import EnableBankingFetcher
    fetcher = EnableBankingFetcher(
        app_id=eb["app_id"],
        private_key_path=str(resolve_path(eb["private_key_path"])),
        state_file=str(resolve_path(eb.get("state_file", "sync/.bank_sync_state.json"))),
        iban_country=_iban_country(cfg),
    )

    ibans = args.ibans or cfg.get("own_ibans", [])
    if not ibans:
        print("Brak kont do autoryzacji. Podaj IBANy jako argumenty lub ustaw own_ibans w config.json.")
        sys.exit(1)

    # Which bank each IBAN belongs to. The account name doesn't tell you — an
    # account called "Joint" can live at any bank — so it comes from config.json:
    # aspsp_names per IBAN, default_aspsp for everything else.
    aspsp_names = cfg.get("aspsp_names") or eb.get("aspsp_names", {})
    default_aspsp = eb.get("default_aspsp") or cfg.get("default_aspsp")
    aspsp_by_iban = {}
    for iban in ibans:
        full_iban = _iban.full_iban(iban, _iban_country(cfg))
        if iban in aspsp_names:
            aspsp_by_iban[full_iban] = aspsp_names[iban]

    fetcher.setup(ibans, aspsp_by_iban, default_aspsp)


def cmd_fetch(args):
    env = load_env()
    cfg = load_config()
    api_key = get_api_key(env)

    # No LLM configured is a supported setup, not an error: the keyword rules
    # still run and whatever they don't match lands in CHECK ME for review.
    # Every review teaches a rule back, so this gets better on its own.
    if not llm.configured() and not api_key:
        print("Bez LLM — kategoryzuję wyłącznie regułami, reszta trafi do CHECK ME.")

    eb = _eb_config(cfg)
    output_dir = resolve_path(cfg["output_dir"])
    account_names = cfg.get("account_names", {})
    days_back = args.days or eb.get("days_back", 30)
    use_smart_window = args.days is None

    from banks.enable_banking import EnableBankingFetcher
    fetcher = EnableBankingFetcher(
        app_id=eb["app_id"],
        private_key_path=str(resolve_path(eb["private_key_path"])),
        state_file=str(resolve_path(eb.get("state_file", "sync/.bank_sync_state.json"))),
        iban_country=_iban_country(cfg),
    )

    ibans = cfg.get("own_ibans", [])
    if not ibans:
        print("Brak own_ibans w config.json.")
        sys.exit(1)

    # Fallback dla kont bez lokalnego stanu last_fetch (np. po reset/migracji):
    # zamiast od razu ciąć do cap_date, wznów od ostatniej transakcji zapisanej
    # w aplikacji dla danego konta.
    fallback_dates = {}
    try:
        last_dates_by_name = budget_client.get("/api/accounts/last-dates")
        for iban, name in account_names.items():
            full_iban = _iban.full_iban(iban, _iban_country(cfg))
            if name in last_dates_by_name:
                fallback_dates[full_iban] = last_dates_by_name[name]
    except Exception as e:
        print(f"  [fallback_dates] aplikacja niedostępna ({e}) — pomijam.")

    if use_smart_window:
        print("Pobieranie transakcji przez Enable Banking (smart window)...\n")
    else:
        print(f"Pobieranie transakcji z ostatnich {days_back} dni przez Enable Banking...\n")
    fetched = fetcher.fetch(ibans, days_back=days_back, use_smart_window=use_smart_window,
                             fallback_dates=fallback_dates, dry_run=args.dry_run)

    # Accounts fetch() itself gave up on (rate limit, expired session, missing
    # config) — as opposed to "already up to date", which isn't in this list.
    # These never got a fetch attempt at all, so they must NOT count toward a
    # successful run, or the watchdog stands down while their data goes stale.
    skipped_ibans = list(getattr(fetcher, "last_skipped", []))
    skip_reasons = getattr(fetcher, "skip_reasons", {})
    failed_accounts = [account_names.get(iban, iban[-4:]) for iban in skipped_ibans]
    # Reason per account name, English — this is what ends up in the Telegram
    # message (master_pi.py), kept separate from the Polish console prints above.
    failed_reasons = {
        account_names.get(iban, iban[-4:]): skip_reasons.get(iban, "unknown reason")
        for iban in skipped_ibans
    }
    accounts_out = []  # dla .last_fetch_summary.json (master_pi.py -> Telegram)

    def _write_summary():
        if args.dry_run:
            return
        (BASE_DIR.parent / ".last_fetch_summary.json").write_text(json.dumps(
            {"date": date.today().isoformat(), "accounts": accounts_out,
             "failed_accounts": failed_accounts, "failed_reasons": failed_reasons},
            ensure_ascii=False,
        ))

    if not fetched:
        _write_summary()
        if failed_accounts:
            print(f"\n❌ SYNC NIEUDANY — {len(failed_accounts)} kont bez próby pobrania: "
                  f"{', '.join(failed_accounts)}. Zobacz błędy wyżej.")
            sys.exit(1)
        print("Brak transakcji (sprawdź czy sesje są aktywne: python main.py fetch-setup).")
        return

    for iban, raw_transactions in fetched:
        account_name = account_names.get(iban, iban[-4:])
        full_iban = _iban.full_iban(iban, _iban_country(cfg))
        print(f"\n── {account_name} ({iban[-10:]}...) ──────────────────────────────────────")
        print(f"  Transakcji: {len(raw_transactions)}")
        account_summary = []

        try:
            transactions = filter_transactions(
                raw_transactions, cfg["own_ibans"], cfg.get("savings_ibans", []),
                cfg.get("filters")
            )
            transfers = sum(1 for tx in transactions if tx.get("_transfer"))
            print(f"  Po filtracji: {len(transactions)} (w tym {transfers} przelewów własnych)")

            if not transactions:
                print("  Brak transakcji do przetworzenia.")
            else:
                print("  Kategoryzuję...")
                categorized = categorize_transactions(transactions, cfg, api_key)
                categorized = split_transactions(categorized, cfg.get("split_rules"))

                print(f"\n  ── Transakcje ──────────────────────────────────────────────")
                for tx in categorized:
                    sign = "+" if tx["amount"] > 0 else "-"
                    amt  = abs(tx["amount"])
                    print(f"  {tx['transaction_date']}  {sign}{amt:>9.2f} PLN  {tx.get('category',''):<30}  {tx['title'][:40]}")
                    tx_type = "Money Transfer" if tx.get("_transfer") else ("Income" if tx["amount"] > 0 else "Expense")
                    if tx.get("_transfer"):
                        other = tx.get("_to_account") or account_names.get(tx.get("counterpart_iban", ""), "") or "?"
                        description = f"{'->' if tx['amount'] < 0 else '<-'} {other}"
                    else:
                        description = (tx.get("title") or "")[:80]
                    account_summary.append({
                        "tx_type": tx_type,
                        "amount": tx["amount"],
                        "description": description,
                    })
                print()

                if args.dry_run:
                    print("  [dry-run] Plik nie został zapisany.")
                else:
                    output_dir.mkdir(parents=True, exist_ok=True)
                    today    = date.today().isoformat()
                    out_path = output_dir / f"moneypro_{account_name}_{today}.csv"
                    out_path.write_text(
                        generate_money_pro_csv(categorized, account_name, account_names),
                        encoding="utf-8",
                    )
                    print(f"  Zapisano: {out_path}")

            # Only now — filtered, categorized, written to disk — is it safe to
            # advance the checkpoint for this account. dry-run never commits,
            # matching fetch-setup/--dry-run's "no persistent side effects" contract.
            if not args.dry_run:
                fetcher.commit_fetch(full_iban)
                accounts_out.append({"name": account_name, "transactions": account_summary})

        except Exception as e:
            failed_accounts.append(account_name)
            failed_reasons[account_name] = str(e)[:150]
            print(f"\n  ❌ BŁĄD przetwarzania konta {account_name}: {e!r}")
            print(f"  ⚠ Checkpoint NIE przesunięty dla tego konta — transakcje zostaną ponowione"
                  f" przy następnym uruchomieniu (nie są zgubione).")

    _write_summary()

    if failed_accounts:
        print(f"\n❌ SYNC CZĘŚCIOWO NIEUDANY — {len(failed_accounts)} kont z błędem lub bez próby "
              f"pobrania: {', '.join(failed_accounts)}. Zobacz błędy wyżej.")
    else:
        print(f"\n✅ Wszystkie konta ({len(fetched)}) przetworzone poprawnie.")

    print("Gotowe! Pliki CSV czekają na push_actuals.py.")

    if failed_accounts:
        sys.exit(1)


# ─── fetch-balances command ───────────────────────────────────────────────────


def cmd_fetch_balances(args):
    """Pobiera salda kont bankowych przez Enable Banking i porównuje z saldem liczonym
    w aplikacji (checkpoint + suma transakcji). NIE nadpisuje salda — aplikacja liczy
    je samo z listy transakcji; to tylko kontrola rozbieżności (brakujące/zdublowane tx).
    Pomija konta Revolut (nie wspierane przez EB w prosty sposób) i konta oszczędnościowe
    (savings_ibans nie mają autoryzacji EB)."""
    import urllib.error
    import urllib.request

    cfg           = load_config()
    eb            = _eb_config(cfg)
    account_names = cfg.get("account_names", {})

    ibans = [
        iban for iban in cfg.get("own_ibans", [])
        if (name := account_names.get(iban, "").strip().lower()) and "revolut" not in name
    ]
    if not ibans:
        print("Brak skonfigurowanych kont bankowych do synchronizacji sald.")
        return

    from banks.enable_banking import EnableBankingFetcher
    fetcher = EnableBankingFetcher(
        app_id=eb["app_id"],
        private_key_path=str(resolve_path(eb["private_key_path"])),
        state_file=str(resolve_path(eb.get("state_file", "sync/.bank_sync_state.json"))),
        iban_country=_iban_country(cfg),
    )

    print("Pobieranie sald przez Enable Banking...\n")
    balances = fetcher.get_balances(ibans)

    try:
        budget_accounts = budget_client.get("/api/accounts")
    except Exception as e:
        print(f"Aplikacja niedostępna ({e}) — pomijam push sald.")
        return
    name_to_account = {a["name"]: a for a in budget_accounts}

    for iban in ibans:
        bare_iban = _iban.bare_iban(iban)
        balance   = balances.get(bare_iban)
        name      = account_names.get(iban)
        if balance is None:
            continue
        account = name_to_account.get(name)
        if not account:
            print(f"  Konto '{name}' nie istnieje w aplikacji — pomijam.")
            continue
        if abs(account["balance"] - balance) < 0.01:
            print(f"  {name}: {balance:.2f} PLN (zgodne)")
            continue
        diff = balance - account["balance"]
        print(f"  ⚠ {name}: app={account['balance']:.2f} PLN, bank={balance:.2f} PLN "
              f"(różnica {diff:+.2f} zł — sprawdź brakujące/zdublowane transakcje)")


# ─── learn command ────────────────────────────────────────────────────────────

LEARN_SYSTEM_PROMPT = """Jesteś ekspertem od kategoryzacji transakcji bankowych.
Analizujesz opisy transakcji z banku i przypisane im kategorie Money Pro.
Generujesz reguły do automatycznej kategoryzacji przyszłych transakcji.
Odpowiadaj WYŁĄCZNIE w formacie JSON. Bez tekstu przed ani po. Bez bloków ```."""


def parse_money_pro_csv(path: Path) -> list:
    transactions = []
    with open(path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f, delimiter=";")
        for row in reader:
            category = (row.get("Category") or "").strip()
            desc     = (row.get("Description") or "").strip()
            tx_type  = (row.get("Transaction Type") or "").strip()

            s = (row.get("Amount") or "0").strip()
            negative = s.startswith("(") and s.endswith(")")
            s = s.strip("()").replace(" ", "").replace("zł", "").replace(",", ".").strip()
            try:
                amount = float(s)
                if negative:
                    amount = -amount
            except ValueError:
                amount = 0.0

            if tx_type in ("Money Transfer", "Opening Balance", "Balance Adjustment"):
                continue
            if not category or not desc:
                continue

            transactions.append({"category": category, "description": desc, "amount": amount})

    return transactions


def generate_new_rules(transactions: list, api_key: str) -> dict:
    by_category = defaultdict(list)
    for tx in transactions:
        by_category[tx["category"]].append(tx["description"])

    examples = {cat: list(dict.fromkeys(descs))[:5] for cat, descs in by_category.items()}

    valid_list = "\n".join(f"  {c}" for c in sorted(valid_categories()))

    prompt = f"""Przeanalizuj poniższe przykłady transakcji bankowych z ich kategoriami Money Pro.
Wygeneruj NOWE reguły kategoryzacji na podstawie tych przykładów.

PRZYKŁADY (kategoria → opisy z banku):
{json.dumps(examples, ensure_ascii=False, indent=2)}

Wygeneruj JSON z nowymi regułami:
{{
  "keywords": {{
    "słowo_kluczowe_lowercase": "Kategoria: Podkategoria"
  }},
  "patterns": [
    {{
      "pattern": "fragment opisu lowercase",
      "category": "Kategoria: Podkategoria",
      "priority": 1
    }}
  ],
  "merchant_map": {{
    "nazwa_firmy_lowercase": "Kategoria: Podkategoria"
  }}
}}

Zasady:
- Używaj WYŁĄCZNIE kategorii z poniższej listy, przepisanych znak w znak
  (odstępy i myślniki też) — inna nazwa = reguła odrzucona:
{valid_list}
- Generuj reguły TYLKO dla wydatków – przychody pomijaj
- Priorytet 1 = bardzo pewna reguła, 2 = prawdopodobna, 3 = możliwa
- Uwzględnij nazwy merchantów z opisów PŁATNOŚĆ KARTĄ
- Krótkie, specyficzne wzorce są lepsze niż ogólne

Zwróć TYLKO JSON."""

    raw = llm.complete(prompt, task="learn", system=LEARN_SYSTEM_PROMPT,
                       max_tokens=3000, api_key=api_key)
    return llm.extract_json(raw)


def drop_invalid_rules(rules: dict) -> dict:
    """Rules are written by an LLM from the corrections CSV, and it invents category
    names ("Child: Nursery: Fees", "Home: Tools&Materials"). apply_rules returns
    the invented name, validate_category rejects it, and the transaction lands in
    CHECK ME — every single month, however often it gets corrected. Drop them at the
    door so a bad rule can never be written to rules.json."""
    kept = {"keywords": {}, "merchant_map": {}, "patterns": []}
    for section in ("keywords", "merchant_map"):
        for key, cat in rules.get(section, {}).items():
            if cat in valid_categories():
                kept[section][key] = cat
            else:
                print(f"  Odrzucam regułę '{key}' — nieistniejąca kategoria '{cat}'")
    for p in rules.get("patterns", []):
        if p.get("category") in valid_categories():
            kept["patterns"].append(p)
        else:
            print(f"  Odrzucam wzorzec '{p.get('pattern')}' — nieistniejąca kategoria '{p.get('category')}'")
    return kept


def merge_rules(existing: dict, new_rules: dict) -> dict:
    new_rules = drop_invalid_rules(new_rules)
    merged = {
        "keywords":     {**existing.get("keywords", {}),     **new_rules.get("keywords", {})},
        "merchant_map": {**existing.get("merchant_map", {}), **new_rules.get("merchant_map", {})},
        "patterns":     [],
    }
    pattern_map = {p["pattern"].lower(): p for p in existing.get("patterns", [])}
    for p in new_rules.get("patterns", []):
        pattern_map[p["pattern"].lower()] = p
    merged["patterns"] = sorted(pattern_map.values(), key=lambda x: x.get("priority", 2))
    return merged


def cmd_learn(args):
    env = load_env()
    cfg = load_config()
    api_key = get_api_key(env)

    # Unlike fetch, learn has nothing to do without a model — writing rules IS
    # the whole command.
    if not llm.configured() and not api_key:
        print("Brak skonfigurowanego LLM (LLM_PROVIDER / ANTHROPIC_API_KEY) — "
              "nie ma z czego generować reguł. Pomijam.")
        sys.exit(0)

    corrections_dir = resolve_path(cfg.get("corrections_dir", "Corrections"))
    if not corrections_dir or not corrections_dir.exists():
        print(f"Brak folderu poprawek: {corrections_dir}")
        print("Ustaw corrections_dir w config.json lub uruchom: python main.py setup")
        sys.exit(1)

    csv_files = sorted(corrections_dir.glob("*.csv"))
    if not csv_files:
        print(f"Brak plików CSV w {corrections_dir}")
        print("Poprawki pobiera push_actuals.py z aplikacji — uruchom go najpierw.")
        sys.exit(0)

    print(f"Znaleziono {len(csv_files)} plik(ów) z poprawkami.\n")

    all_transactions = []
    for csv_file in csv_files:
        txs = parse_money_pro_csv(csv_file)
        print(f"  {csv_file.name}: {len(txs)} transakcji")
        all_transactions.extend(txs)

    if not all_transactions:
        print("Brak transakcji z kategoriami w plikach.")
        sys.exit(0)

    print(f"\nŁącznie: {len(all_transactions)} transakcji.")
    print("Generuję nowe reguły przez Claude Sonnet...")

    new_rules = generate_new_rules(all_transactions, api_key)
    print(f"  → {len(new_rules.get('keywords', {}))} keywords, "
          f"{len(new_rules.get('patterns', []))} patterns, "
          f"{len(new_rules.get('merchant_map', {}))} merchant mappings")

    existing = load_rules()
    merged   = merge_rules(existing, new_rules)

    print(f"\nPo połączeniu: {len(merged['keywords'])} keywords, "
          f"{len(merged['patterns'])} patterns, "
          f"{len(merged['merchant_map'])} merchant mappings")

    if args.dry_run:
        print("\n[dry-run] rules.json nie został zaktualizowany.")
        return

    save_rules(merged)
    print(f"\nZaktualizowano: {RULES_FILE}")

    processed_dir = corrections_dir / "processed"
    processed_dir.mkdir(exist_ok=True)
    for csv_file in csv_files:
        shutil.move(str(csv_file), processed_dir / csv_file.name)
        print(f"Przeniesiono: {csv_file.name} → processed/")

    print("\nGotowe! Nowe reguły aktywne przy następnym: python main.py sync")

# ─── Entry point ──────────────────────────────────────────────────────────────

def main():
    log_path = start_logging("sync", BASE_DIR.parent)
    print(f"(log: {log_path})")

    ok, missing = check_config_ready()
    if not ok:
        print("Brak konfiguracji:")
        for m in missing:
            print(f"  • {m}")
        print("\nUruchom: python main.py setup\n")

    parser = argparse.ArgumentParser(
        description="money-badger sync – bank → categorize → budget app",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Komendy:
  setup          – wizard konfiguracji (pierwsze uruchomienie)
  fetch          – pobierz transakcje przez Enable Banking → CSV
  fetch-setup    – jednorazowa autoryzacja kont w Enable Banking
  fetch-balances – pobierz salda kont i wyślij do appki
  learn          – zaktualizuj rules.json z poprawek zrobionych w appce""",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("setup", help="wizard konfiguracji")

    p_learn = sub.add_parser("learn", help="zaktualizuj reguły z poprawek zrobionych w aplikacji")
    p_learn.add_argument("--dry-run", action="store_true", help="nie zapisuj rules.json")

    p_fetch = sub.add_parser("fetch", help="pobierz transakcje przez Enable Banking API")
    p_fetch.add_argument("--days", type=int, help="ile dni wstecz (domyślnie z config.json lub 30)")
    p_fetch.add_argument("--dry-run", action="store_true", help="nie zapisuj plików")

    p_fetch_setup = sub.add_parser("fetch-setup", help="jednorazowa autoryzacja kont w Enable Banking")
    p_fetch_setup.add_argument("ibans", nargs="*", help="IBANy do autoryzacji (domyślnie z config.json)")

    sub.add_parser("fetch-balances", help="pobierz salda kont bankowych i wyślij do aplikacji")

    args = parser.parse_args()

    if args.command == "setup":
        run_setup()
    elif args.command == "learn":
        if not ok:
            sys.exit(1)
        cmd_learn(args)
    elif args.command == "fetch":
        cmd_fetch(args)
    elif args.command == "fetch-setup":
        cmd_fetch_setup(args)
    elif args.command == "fetch-balances":
        cmd_fetch_balances(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
