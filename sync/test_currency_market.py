#!/usr/bin/env python3
"""Self-check for the parts of main.py that must not assume a country —
amount parsing, the machine-readable CSV, market hint packs, and category names
reaching the model untouched whatever language they are in.

python3 sync/test_currency_market.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import main

# Categories as a non-English user would have named them. Nothing in the pipeline
# may translate, normalize or re-case them — a rewritten name is a rejected rule.
CATEGORIES = (
    "EXPENSES:\n  Wydatki: Żywność | Paliwo\n\nINCOME:\n  Wynagrodzenie",
    {"Wydatki: Żywność", "Wydatki: Paliwo", "Wynagrodzenie"},
)


def fake_tx(amount=-49.99, title="PŁATNOŚĆ KARTĄ 1234 ŻABKA NANO", **kw):
    tx = {
        "amount": amount,
        "transaction_date": "2026-08-13",
        "title": title,
        "counterpart": "Żabka Polska sp. z o.o.",
        "counterpart_iban": "",
        "category": "Wydatki: Żywność",
        "note": "",
        "_transfer": False,
        "_savings": False,
        "_to_account": "",
    }
    tx.update(kw)
    return tx


def capture_prompt(run):
    """Call run() with the model stubbed out; return the prompt it was given."""
    seen = {}

    def stub(prompt, task="categorize", system=None, max_tokens=4000, api_key=None):
        seen["prompt"] = prompt
        seen["system"] = system
        return "[]"

    real_complete, real_categories = main.llm.complete, main._categories
    main.llm.complete, main._categories = stub, CATEGORIES
    try:
        run()
    finally:
        main.llm.complete, main._categories = real_complete, real_categories
    return seen


# ─── amounts ──────────────────────────────────────────────────────────────────

def test_parse_amount_reads_any_currency_and_separator():
    """The corrections CSV is formatted by the app for whatever currency it is set
    to, so the parser may not assume a symbol, a separator or their order."""
    cases = {
        "1 234,56 zł": 1234.56,   # what a Polish install writes
        "$1,234.56":   1234.56,   # symbol in front, comma groups thousands
        "1.234,56 €":  1234.56,   # dot groups thousands
        "1234.56":     1234.56,   # what this script now writes
        "1 234.56 CHF": 1234.56,  # ISO code instead of a symbol
        "49,00 zł":    49.0,
        "12,5":        12.5,      # one decimal digit
        "1,234":       1234.0,    # three digits after the comma = thousands, not cents
        "1 234":       1234.0,
        "-1234.56":    -1234.56,
        "(1 234,56 zł)": -1234.56,  # accounting notation for a minus
        "":            0.0,
        "n/a":         0.0,
    }
    for text, expected in cases.items():
        assert main.parse_amount(text) == expected, (text, main.parse_amount(text))


def test_csv_amount_is_a_bare_number():
    """No currency symbol and no thousands separator in the file: every consumer
    parses it straight back, and displaying money is the app's job."""
    csv = main.generate_money_pro_csv([fake_tx(amount=-1234.5)], "Main", {})
    amount_cell = csv.splitlines()[1].split(";")[1]
    assert amount_cell == "1234.50", amount_cell
    assert main.parse_amount(amount_cell) == 1234.5


def test_csv_amount_survives_the_round_trip_to_learn():
    """generate → parse is the actual path a corrected transaction takes."""
    for amount in (-0.99, -1234.5, -1000000.0, -49.99):
        csv = main.generate_money_pro_csv([fake_tx(amount=amount)], "Main", {})
        assert main.parse_amount(csv.splitlines()[1].split(";")[1]) == abs(amount)


def test_currency_symbol_falls_back_to_none_when_the_app_is_down():
    """The symbol is cosmetic — an unreachable app must not fail a sync over it."""
    real_get, main._currency = main.budget_client.get, None
    try:
        main.budget_client.get = lambda *a, **kw: (_ for _ in ()).throw(OSError("down"))
        assert main.currency() == ""

        main._currency = None
        main.budget_client.get = lambda *a, **kw: {"currency": "$", "locale": "en-US"}
        assert main.currency() == "$"
        main.budget_client.get = lambda *a, **kw: {"currency": "kr"}
        assert main.currency() == "$"  # cached for the run, one call per process
    finally:
        main.budget_client.get, main._currency = real_get, None


# ─── market hint packs ────────────────────────────────────────────────────────

def test_no_market_configured_means_no_hints():
    assert main.market_hints({}, "learn_hints") == []
    assert main.market_hints({"market": ""}, "learn_hints") == []


def test_missing_market_pack_is_not_an_error():
    """A country nobody wrote a pack for still syncs — just without hints."""
    assert main.market_hints({"market": "nowhere"}, "learn_hints") == []


def test_polish_pack_is_readable_and_only_answers_for_keys_it_has():
    hints = main.market_hints({"market": "pl"}, "learn_hints")
    assert hints and all(isinstance(h, str) for h in hints)
    assert main.market_hints({"market": "pl"}, "categorize_hints") == []


def test_learn_prompt_works_with_and_without_a_pack():
    txs = [{"category": "Wydatki: Żywność", "description": "PŁATNOŚĆ KARTĄ ŻABKA", "amount": -49.99}]

    plain = capture_prompt(lambda: main.generate_new_rules(txs, "key"))["prompt"]
    assert "PŁATNOŚĆ KARTĄ" not in plain.split("Rules:")[1], "hint leaked into a pack-less run"

    hints = main.market_hints({"market": "pl"}, "learn_hints")
    with_pack = capture_prompt(lambda: main.generate_new_rules(txs, "key", hints))["prompt"]
    for hint in hints:
        assert f"\n- {hint}" in with_pack, hint


# ─── category names are never rewritten ───────────────────────────────────────

def test_category_names_reach_the_categorize_prompt_verbatim():
    seen = capture_prompt(lambda: main._categorize_batch([fake_tx()], "key"))
    prompt = seen["prompt"]
    assert "Wydatki: Żywność | Paliwo" in prompt          # the tree, as the user named it
    assert "PŁATNOŚĆ KARTĄ 1234 ŻABKA NANO" in prompt     # the bank's own wording, untranslated
    assert "\\u017b" not in prompt                        # not escaped away by json.dumps


def test_category_names_reach_the_learn_prompt_verbatim():
    txs = [{"category": "Wydatki: Żywność", "description": "ŻABKA NANO 1234", "amount": -49.99}]
    prompt = capture_prompt(lambda: main.generate_new_rules(txs, "key"))["prompt"]
    for name in sorted(CATEGORIES[1]):
        assert f"  {name}" in prompt, name
    assert "ŻABKA NANO 1234" in prompt


if __name__ == "__main__":
    test_parse_amount_reads_any_currency_and_separator()
    test_csv_amount_is_a_bare_number()
    test_csv_amount_survives_the_round_trip_to_learn()
    test_currency_symbol_falls_back_to_none_when_the_app_is_down()
    test_no_market_configured_means_no_hints()
    test_missing_market_pack_is_not_an_error()
    test_polish_pack_is_readable_and_only_answers_for_keys_it_has()
    test_learn_prompt_works_with_and_without_a_pack()
    test_category_names_reach_the_categorize_prompt_verbatim()
    test_category_names_reach_the_learn_prompt_verbatim()
    print("OK — currency and market-hint checks passed")
