#!/usr/bin/env python3
"""Self-check for split_transactions() — run directly, no framework/fixtures.

python3 sync/test_split_rules.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from main import split_transactions

IBAN = "12345678901234567890123456"  # made up

# A bundled transfer: fixed 200.00 insurance + fixed 500.00 into savings + a
# variable rent remainder (600-900). The range brackets the possible totals.
RULES = [{
    "counterpart_iban": IBAN,
    "total_range": [1300.00, 1600.00],
    "parts": [
        {"amount": 200.00, "category": "Housing: Insurance", "label": "insurance"},
        {"amount": 500.00, "category": "Savings", "label": "savings"},
        {"amount": "remainder", "category": "Housing: Rent / Mortgage", "label": "rent"},
    ],
}]


def fake_tx(amount, counterpart_iban=IBAN):
    return {
        "amount": amount,
        "title": "Przelew środków własnych — między twoimi kontami",
        "counterpart": "Konto zewnętrzne",
        "counterpart_iban": counterpart_iban,  # already normalized (no "PL", no spaces), matches normalize_iban() output
        "transaction_date": "2026-07-10",
        "category": "",
        "note": "",
        "_transfer": True,
        "_savings": False,
        "_to_account": "",
    }


def test_splits_into_three_matching_categories_and_sum():
    original_amount = -1300.00  # 600.00 rent + 500.00 savings + 200.00 insurance
    result = split_transactions([fake_tx(original_amount)], RULES)

    assert len(result) == 3, f"expected 3 rows, got {len(result)}"

    cats = {r["category"] for r in result}
    assert cats == {"Housing: Rent / Mortgage", "Savings", "Housing: Insurance"}, cats

    total = sum(r["amount"] for r in result)
    assert abs(total - original_amount) < 0.001, f"sum mismatch: {total} != {original_amount}"

    for r in result:
        assert r["_transfer"] is False
        assert r["_to_account"] == ""

    insurance_row = next(r for r in result if r["category"] == "Housing: Insurance")
    assert abs(insurance_row["amount"] - (-200.00)) < 0.001

    remainder_row = next(r for r in result if r["category"] == "Housing: Rent / Mortgage")
    assert abs(remainder_row["amount"] - (-600.00)) < 0.001


def test_out_of_range_amount_is_left_untouched():
    tx = fake_tx(-50.0)  # too small to be the bundled transfer
    assert split_transactions([tx], RULES) == [tx]


def test_non_matching_iban_is_left_untouched():
    tx = fake_tx(-1300.00, counterpart_iban="65432109876543210987654321")
    assert split_transactions([tx], RULES) == [tx]


def test_no_rules_configured_is_a_passthrough():
    """The default for everyone who hasn't configured a split — must not touch anything."""
    tx = fake_tx(-1300.00)
    assert split_transactions([tx], None) == [tx]
    assert split_transactions([tx], []) == [tx]


def test_splits_even_when_not_flagged_as_transfer():
    # Regression test (2026-07-12): the destination is an external bank, never in
    # own_ibans/savings_ibans, so filter_transactions() never sets _transfer=True for
    # this transfer — matching used to require _transfer=True and silently never fired.
    # The split must trigger on IBAN + amount range alone, regardless of the flag.
    tx = fake_tx(-1300.00)
    tx["_transfer"] = False
    result = split_transactions([tx], RULES)
    assert len(result) == 3, f"expected split into 3 rows even with _transfer=False, got {len(result)}"


def test_incoming_transfer_is_never_split():
    """Rules only ever carve up money leaving an account."""
    tx = fake_tx(1300.00)
    assert split_transactions([tx], RULES) == [tx]


if __name__ == "__main__":
    test_splits_into_three_matching_categories_and_sum()
    test_out_of_range_amount_is_left_untouched()
    test_non_matching_iban_is_left_untouched()
    test_no_rules_configured_is_a_passthrough()
    test_splits_even_when_not_flagged_as_transfer()
    test_incoming_transfer_is_never_split()
    print("OK — wszystkie testy split_transactions przeszły")
