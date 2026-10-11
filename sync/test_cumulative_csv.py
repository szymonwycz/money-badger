"""Offline tests for the cumulative account/day CSV outbox."""
import csv
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import main as sync_main


ACCOUNT = "Offline account"


def bank_tx(eb_id, *, amount=-10.0, title="Synthetic merchant",
            category="Groceries", transfer=False):
    return {
        "_eb_id": eb_id,
        "amount": amount,
        "transaction_date": "2032-10-09",
        "title": title,
        "counterpart": "Synthetic counterpart",
        "counterpart_iban": "99990000111122223333444455",
        "category": category,
        "note": "",
        "_transfer": transfer,
        "_to_account": "Destination" if transfer else "",
    }


def csv_text(*transactions):
    return sync_main.generate_money_pro_csv(list(transactions), ACCOUNT, {})


def rows(path):
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f, delimiter=";"))


def test_plain_row_stays_compatible_and_carries_check_number():
    generated = csv_text(bank_tx("plain-id"))
    assert generated.splitlines()[1] == (
        "09/10/2032, 01:00;10.00;Offline account;;;;Groceries;"
        "Synthetic merchant;Expense;;plain-id;;"
    )


def test_full_counterpart_iban_uses_agent_without_changing_csv_shape():
    transaction = bank_tx("iban-id")
    transaction["_counterpart_iban_full"] = "GB82WEST12345698765432"
    parsed = list(csv.DictReader(csv_text(transaction).splitlines(), delimiter=";"))[0]

    assert parsed["Agent"] == "GB82WEST12345698765432"
    assert parsed["Check #"] == "iban-id"


def test_goal_discovery_candidate_uses_class_without_changing_csv_shape():
    transaction = bank_tx("candidate-id")
    transaction["_force_check_me"] = True
    parsed = list(csv.DictReader(csv_text(transaction).splitlines(), delimiter=";"))[0]

    assert parsed["Class"] == "goal-discovery"
    assert parsed["Check #"] == "candidate-id"


def test_counterpart_iban_does_not_change_check_number_deduplication(tmp_path):
    path = tmp_path / "daily.csv"
    first = bank_tx("same-id")
    first["_counterpart_iban_full"] = "GB82WEST12345698765432"
    replay = bank_tx("same-id")
    replay["_counterpart_iban_full"] = "DE89370400440532013000"

    assert sync_main._merge_daily_csv(path, csv_text(first)) == 1
    assert sync_main._merge_daily_csv(path, csv_text(replay)) == 0
    assert rows(path)[0]["Agent"] == "GB82WEST12345698765432"


def test_semicolons_quotes_and_newlines_round_trip_without_shifting_columns(tmp_path):
    note = bank_tx("note")
    note["note"] = "first clause; second clause"
    quoted = bank_tx("quoted", title='Merchant "quoted"; branch')
    multiline = bank_tx("multiline", title="Line one\r\nLine two\nLine three")
    path = tmp_path / "daily.csv"

    generated = csv_text(note, quoted, multiline)
    assert sync_main._merge_daily_csv(path, generated) == 3
    parsed = rows(path)
    assert [row["Description"] for row in parsed] == [
        "Synthetic merchant [first clause; second clause]",
        'Merchant "quoted"; branch',
        "Line one Line two Line three",
    ]
    assert [row["Transaction Type"] for row in parsed] == ["Expense"] * 3
    assert [row["Check #"] for row in parsed] == ["note", "quoted", "multiline"]
    assert len(generated.splitlines()) == 4
    assert "\r" not in generated


def test_each_pass_appends_only_unseen_ids_and_preserves_existing_bytes(tmp_path):
    path = tmp_path / "daily.csv"
    assert sync_main._merge_daily_csv(path, csv_text(bank_tx("first"))) == 1
    original = path.read_bytes()
    assert sync_main._merge_daily_csv(
        path, csv_text(bank_tx("first"), bank_tx("second", title="Second"))) == 1
    assert path.read_bytes().startswith(original + b"\n")
    assert [row["Check #"] for row in rows(path)] == ["first", "second"]
    assert sync_main._merge_daily_csv(path, csv_text(bank_tx("second"))) == 0


def test_legacy_row_without_check_number_is_retained(tmp_path):
    path = tmp_path / "daily.csv"
    header = csv_text(bank_tx("new-id")).splitlines()[0]
    legacy = "09/10/2032, 01:00;10.00;Offline account;;;;Groceries;legacy;Expense;;;;"
    path.write_text(f"{header}\n{legacy}", encoding="utf-8")
    assert sync_main._merge_daily_csv(path, csv_text(bank_tx("new-id"))) == 1
    assert len(rows(path)) == 2


def test_new_eb_rows_without_check_number_are_rejected(tmp_path):
    tx = bank_tx("")
    with pytest.raises(ValueError, match="empty Check"):
        sync_main._merge_daily_csv(tmp_path / "daily.csv", csv_text(tx))


def test_split_parts_get_stable_suffixes_and_replay_does_not_multiply(tmp_path):
    source = bank_tx("bundle", amount=-30.0, title="Bundle")
    source["counterpart_iban"] = "1234567890"
    rules = [{
        "counterpart_iban": "1234567890",
        "total_range": [29.0, 31.0],
        "parts": [
            {"amount": 10.0, "category": "Groceries", "label": "part one"},
            {"amount": "remainder", "category": "Pets", "label": "part two"},
        ],
    }]
    parts = sync_main.split_transactions([source], rules)
    path = tmp_path / "daily.csv"
    batch = csv_text(*parts)
    assert sync_main._merge_daily_csv(path, batch) == 2
    assert sync_main._merge_daily_csv(path, batch) == 0
    assert [row["Check #"] for row in rows(path)] == ["bundle#0", "bundle#1"]
