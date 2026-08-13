"""Guards for transaction filtering and rule validity.

These use their own rules and category list rather than the installed config, so
they say the same thing on every machine.
"""
import json
from pathlib import Path

import pytest

import main

# Stand in for the live category tree the app would serve, so validate_category()
# doesn't need a running server.
CATEGORIES = {
    "Groceries", "Transport", "Transport: Fuel", "Children",
    "Children: Childcare", "CHECK ME",
}


@pytest.fixture(autouse=True)
def _categories():
    """Per test, not at import: main._categories is module state, and another test
    file in the same run sets its own — which used to leak in here."""
    main._categories = ("EXPENSES:\n  Groceries\n", CATEGORIES)
    yield
    main._categories = None


RULES = {
    "keywords": {"bp ": "Transport: Fuel"},
    "merchant_map": {},
    "patterns": [
        {"pattern": "kindergarten", "category": "Children: Childcare", "priority": 2},
    ],
}

# Phrases a Polish bank uses; every deployment configures its own.
FILTERS = {
    "own_transfer_titles": ["między twoimi kontami", "between your own accounts"],
    "transfer_to_account": [
        {"match": ["przelew kartą", "revolut"], "account": "Revolut"},
        {"match": ["wypłata blik"], "account": "Cash"},
    ],
    "savings_withdrawal_titles": ["wypłata z celu"],
    "skip_incoming_titles": ["top-up"],
}


def _tx(title, amount, iban="", counterpart=""):
    return {"title": title, "amount": amount, "counterpart_iban": iban,
            "counterpart": counterpart}


def test_card_topup_incoming_leg_is_dropped():
    """The receiving side of an Apple Pay top-up carries no IBAN, so it used to land
    as income while the funding account booked the same money as a transfer."""
    out = main.filter_transactions([_tx("Apple Pay Top-Up by *1234", 3000.0)], [], [], FILTERS)
    assert out == [], out


def test_card_topup_outgoing_leg_survives_as_transfer():
    out = main.filter_transactions(
        [_tx("DOP. VISA 000000******1234 PRZELEW KARTĄ 200.00 PLN  Revolut**5678*", -200.0)],
        [], [], FILTERS)
    assert len(out) == 1 and out[0]["_transfer"] and out[0]["_to_account"] == "Revolut"


def test_cash_withdrawal_becomes_a_transfer_to_the_cash_account():
    out = main.filter_transactions([_tx("WYPŁATA BLIK 200 PLN", -200.0)], [], [], FILTERS)
    assert len(out) == 1 and out[0]["_to_account"] == "Cash"


def test_no_filters_configured_leaves_transactions_alone():
    """The default for a fresh install: nothing is reclassified, nothing is dropped."""
    out = main.filter_transactions([_tx("Apple Pay Top-Up by *1234", 3000.0)], [], [])
    assert len(out) == 1 and not out[0]["_transfer"]


def test_pattern_naming_a_real_category_survives_validation():
    """A pattern is only useful if the category it names exists — otherwise
    validate_category turns every match into CHECK ME."""
    cat = main.apply_rules("Kindergarten fee - August 2026", "Little Owls,", -950.0, RULES)
    assert main.validate_category(cat) == "Children: Childcare", cat


def test_invented_categories_never_reach_rules_json():
    merged = main.merge_rules(
        {"keywords": {}, "merchant_map": {}, "patterns": []},
        {"keywords": {"nursery": "Children: Nursery: Fees", "lidl": "Groceries"},
         "merchant_map": {}, "patterns": [{"pattern": "x", "category": "No Such Category", "priority": 1}]},
    )
    assert merged["keywords"] == {"lidl": "Groceries"}
    assert merged["patterns"] == []


def test_keyword_does_not_match_across_a_word_boundary():
    """The 'bp ' keyword used to also match the trailing space of 'exchanged to gbp'."""
    assert main.apply_rules("Exchanged to GBP", "", -750.0, RULES) is None
    cat = main.apply_rules("VISA PLAT 000000******1234 PŁATNOŚĆ KARTĄ BP MAIN STREET", "", -230.0, RULES)
    assert main.validate_category(cat) == "Transport: Fuel", cat


def test_example_rules_file_is_well_formed():
    """It is what a new install copies, so a typo here breaks every first run."""
    rules = json.loads((Path(main.__file__).parent / "rules.example.json").read_text(encoding="utf-8"))
    for section in ("keywords", "merchant_map"):
        assert isinstance(rules[section], dict)
        assert all(isinstance(k, str) and isinstance(v, str) for k, v in rules[section].items())
    for p in rules["patterns"]:
        assert set(p) == {"pattern", "category", "priority"}, p
