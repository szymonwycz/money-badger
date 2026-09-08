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


def test_a_marketplace_charge_waits_for_the_email_instead_of_being_guessed():
    """The card charge names the marketplace, not the goods — sync/order_match.py
    fills that in from the order confirmation. With no rule matching, apply_rules
    returned None and the charge went to the LLM, which guessed. Any guess sets
    category_id, which drops the row out of /api/transactions/unreviewed — the list
    order_match searches — so guessing quietly prevented the enrichment.

    The label must stay truthy: categorize_transactions only skips the LLM for a
    truthy result, so returning None here would restore the guessing in silence."""
    empty = {"keywords": {}, "merchant_map": {}, "patterns": []}
    shops = ("Allegro", "Amazon")
    charge = "VISA PLAT 000000******1234 P\u0141ATNO\u015a\u0106 KART\u0104 122.35 PLN  Allegro Poznan"

    assert main.apply_rules(charge, "", -122.35, empty, shops) == "CHECK ME"
    assert main.apply_rules("AMAZON EU SARL", "", -49.99, empty, shops) == "CHECK ME"

    # A learned rule must not win over the guard either.
    with_rule = {"keywords": {"allegro": "Groceries"}, "merchant_map": {}, "patterns": []}
    assert main.apply_rules(charge, "", -122.35, with_rule, shops) == "CHECK ME"

    # A shop that is not configured is none of the guard's business.
    assert main.apply_rules(charge, "", -122.35, empty, ("Amazon",)) is None


def test_shop_names_falls_back_to_the_same_default_as_order_match():
    assert main.shop_names({}) == ("Allegro",)
    assert main.shop_names({"order_matching": {"shops": {"Amazon": {}, "eBay": {}}}}) \
        == ("Amazon", "eBay")


def test_rules_do_not_match_inside_a_longer_word():
    """Plain substring matching lets a two-letter merchant hijack a product name:
    "bp" matches the "Mbps" in a network switch, "leasing" matches the Polish
    "Poleasingowy" (second-hand ex-lease). Invisible while only bank descriptors
    were rated; a live problem once order_match started rating product names."""
    rules = {
        "keywords": {"leasing": "Car: Leasing"},
        "merchant_map": {"bp": "Car: Fuel"},
        "patterns": [],
    }
    assert main.apply_rules("Switch TP-LINK TL-SG108 (8x 10/100/1000Mbps)", "",
                            -101.80, rules) is None
    assert main.apply_rules("Poleasingowy micro Dell 7060 Tiny i5", "",
                            -1998.0, rules) is None

    # The rules these keywords were written for still fire.
    assert main.apply_rules("CARD PAYMENT BP FUEL STATION", "",
                            -250.0, rules) == "Car: Fuel"
    assert main.apply_rules("LEASING INSTALMENT", "",
                            -1200.0, rules) == "Car: Leasing"


def test_stem_rules_still_match_inflected_endings():
    """Only the front is anchored, on purpose: learned rules are stems and the
    endings change. Anchoring the tail too would silently kill them."""
    rules = {"keywords": {"uszczelka": "Home: Small accessories"},
             "merchant_map": {}, "patterns": []}
    assert main.apply_rules("USZCZELKA KLINOWA GRAFIT", "", -84.25,
                            rules) == "Home: Small accessories"
    assert main.apply_rules("zestaw uszczelkami do okien", "", -84.25,
                            rules) == "Home: Small accessories"
