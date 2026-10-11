"""Offline precedence matrix for goal/subaccount IBAN discovery and transfers."""
import csv
import io

import pytest

import main


OWN = "PL11111111111111111111111111"
GOAL = "PL22222222222222222222222222"
SAVINGS = "PL33333333333333333333333333"

FILTERS = {
    "own_transfer_titles": ["between your own accounts"],
    "transfer_to_account": [{"match": ["cash withdrawal"], "account": "Cash"}],
    "savings_withdrawal_titles": ["from old savings"],
    "skip_incoming_titles": ["top-up"],
    "goal_transfer_titles": ["PAY INTO GOAL"],
}


def _tx(title="ordinary", amount=-10.0, iban=GOAL, counterpart="Merchant"):
    return {
        "title": title,
        "amount": amount,
        "counterpart_iban": iban[2:] if iban.startswith("PL") else iban,
        "_counterpart_iban_full": iban,
        "counterpart": counterpart,
        "transaction_date": "2032-10-09",
        "_eb_id": "eb-1",
    }


def _filter(tx, *, goals=None, own=None, savings=None, holders=None, non_goals=None):
    return main.filter_transactions(
        [tx], own if own is not None else [OWN], savings or [], FILTERS,
        goal_accounts=goals or {}, iban_country="PL", account_holders=holders or [],
        non_goal_ibans=non_goals or [],
    )


def test_mapped_iban_is_title_independent_in_both_directions():
    outgoing = _filter(_tx(title="unrelated debit", amount=-25), goals={GOAL: "Holiday"})[0]
    assert outgoing["_transfer"] is True
    assert outgoing["_to_account"] == "Holiday"
    assert outgoing["_goal_withdrawal"] is False

    incoming = _filter(_tx(title="interest-looking credit", amount=25),
                       goals={GOAL: "Holiday"})[0]
    assert incoming["_transfer"] is True
    assert incoming["_from_account"] == "Holiday"
    assert incoming["_goal_withdrawal"] is True


def test_explicit_transfer_rule_keeps_precedence_over_mapped_iban():
    row = _filter(_tx(title="cash withdrawal", amount=-25),
                  goals={GOAL: "Holiday"})[0]
    assert row["_to_account"] == "Cash"
    assert not row.get("_goal_withdrawal")


@pytest.mark.parametrize("title,amount", [
    ("from old savings", 25),
    ("TOP-UP", 25),
])
def test_mapped_iban_precedes_legacy_savings_and_skip_titles(title, amount):
    row = _filter(_tx(title=title, amount=amount), goals={GOAL: "Holiday"})[0]
    assert row["_transfer"] is True
    assert row["_from_account"] == "Holiday"


def test_own_iban_is_never_reinterpreted_as_a_goal():
    row = _filter(_tx(amount=-25, iban=OWN), goals={OWN: "Wrong"})[0]
    assert row["_transfer"] is True
    assert row["_to_account"] == ""
    assert not row.get("_goal_withdrawal")


def test_existing_savings_skip_and_own_branches_precede_discovery():
    savings = _filter(_tx(title="pay into goal", iban=SAVINGS, counterpart="Alex Example"),
                      savings=[SAVINGS], holders=["Alex Example"])[0]
    assert savings["_savings"] is True
    assert not savings.get("_force_check_me")

    skipped = _filter(_tx(title="TOP-UP pay into goal", amount=10,
                          counterpart="Alex Example"), holders=["Alex Example"])
    assert skipped == []

    own_incoming = _filter(_tx(title="pay into goal", amount=10, iban=OWN,
                               counterpart="Alex Example"), holders=["Alex Example"])
    assert own_incoming == []


@pytest.mark.parametrize("title,counterpart", [
    ("PAY INTO GOAL", "Unrelated Merchant"),
    ("anything", "ALÉX EXAMPLE, Main Street 10"),
])
def test_unknown_external_iban_is_forced_to_check_me_by_either_signal(
        monkeypatch, title, counterpart):
    row = _filter(_tx(title=title, counterpart=counterpart), holders=["Alex Example"])[0]
    assert row["_force_check_me"] is True

    monkeypatch.setattr(main, "_categories",
                        ("EXPENSES:\n  Groceries", {"Groceries", "CHECK ME"}))
    monkeypatch.setattr(main, "load_rules", lambda: {
        "keywords": {"anything": "Groceries", "pay into goal": "Groceries"},
        "merchant_map": {}, "patterns": [],
    })
    monkeypatch.setattr(main.llm, "configured", lambda: True)
    monkeypatch.setattr(main, "_categorize_batch",
                        lambda *_args, **_kwargs: pytest.fail("LLM must not run"))
    assert main.categorize_transactions([row], {"own_ibans": []}, "")[0]["category"] == "CHECK ME"


def test_unknown_external_iban_without_signal_keeps_normal_classification():
    row = _filter(_tx(title="ordinary", counterpart="Merchant"))[0]
    assert row["_transfer"] is False
    assert not row.get("_force_check_me")


@pytest.mark.parametrize("title,counterpart", [
    ("PAY INTO GOAL", "Unrelated Merchant"),
    ("anything", "Alex Example, Main Street 10"),
])
def test_reviewed_non_goal_iban_suppresses_only_discovery(title, counterpart):
    row = _filter(
        _tx(title=title, counterpart=counterpart), holders=["Alex Example"],
        non_goals=[GOAL],
    )[0]
    assert row["_transfer"] is False
    assert not row.get("_force_check_me")


def test_learned_rule_runs_after_non_goal_decision(monkeypatch):
    row = _filter(
        _tx(title="PAY INTO GOAL regular loan overpayment"), non_goals=[GOAL],
    )[0]
    monkeypatch.setattr(main, "_categories",
                        ("EXPENSES:\n  Loans", {"Loans", "CHECK ME"}))
    monkeypatch.setattr(main, "load_rules", lambda: {
        "keywords": {"loan overpayment": "Loans"},
        "merchant_map": {}, "patterns": [],
    })
    monkeypatch.setattr(main, "_categorize_batch",
                        lambda *_args, **_kwargs: pytest.fail("LLM must not run"))
    categorized = main.categorize_transactions([row], {"own_ibans": []}, "")
    assert categorized[0]["category"] == "Loans"


def test_unavailable_goal_map_is_non_fatal(monkeypatch, capsys):
    monkeypatch.setattr(main.budget_client, "API_TOKEN", "configured")
    monkeypatch.setattr(main.budget_client, "get", lambda *_args: (_ for _ in ()).throw(OSError("down")))
    assert main._load_goal_accounts() == ({}, set())
    assert "discovery candidates still go to CHECK ME" in capsys.readouterr().out


def test_missing_api_token_reports_that_saved_iban_decisions_are_disabled(
        monkeypatch, capsys):
    monkeypatch.setattr(main.budget_client, "API_TOKEN", "")
    monkeypatch.setattr(main.budget_client, "get",
                        lambda *_args: pytest.fail("endpoint must not be called without token"))
    assert main._load_goal_accounts() == ({}, set())
    message = capsys.readouterr().out
    assert "goal IBAN mapping is disabled" in message
    assert "even when browser login is disabled" in message


def test_goal_state_loader_reads_positive_and_negative_decisions(monkeypatch):
    monkeypatch.setattr(main.budget_client, "API_TOKEN", "configured")
    monkeypatch.setattr(main.budget_client, "get", lambda *_args: {
        "accounts": {GOAL: "Holiday"},
        "non_goal_ibans": [SAVINGS],
    })
    assert main._load_goal_accounts() == ({GOAL: "Holiday"}, {SAVINGS})


def test_goal_withdrawal_csv_runs_from_asset_to_current_account():
    row = _filter(_tx(amount=25), goals={GOAL: "Holiday"})[0]
    row["category"] = ""
    parsed = list(csv.reader(io.StringIO(
        main.generate_money_pro_csv([row], "Main account", {})), delimiter=";"))
    assert parsed[1][2] == "Holiday"
    assert parsed[1][4] == "Main account"
    assert parsed[1][8] == "Money Transfer"
