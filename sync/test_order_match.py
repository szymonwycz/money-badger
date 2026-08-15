"""Checks for the multi-shop side of order_match.py: reading an order out of
an email with the model, and matching one to a transaction.

No network and no model — the LLM call and the app's API are stubbed. The
Allegro parser has its own checks in test_allegro_html.py.
"""
import json

import order_match as om

OPTS = om.options({"order_matching": {
    "local_currency": "PLN",
    "fx_rates": {"EUR": 4.30},
    "fx_tolerance_pct": 5,
    "shops": {"Allegro": {"folder": "Allegro"}, "Amazon": {"folder": "Amazon"}},
}})

CATEGORIES = [{"name": "Home", "children": [{"name": "Small accessories", "id": 7}]}]

GOOD_ANSWER = {
    "is_order": True,
    "order_id": "302-1234567-1234567",
    "order_date": "2026-03-05",
    "total": 49.99,
    "currency": "EUR",
    "items": ["USB-C cable 2 m", {"name": "Phone case"}],
}


def stub_llm(monkeypatch, answer):
    """The model always replies with `answer`, fenced the way models like to."""
    raw = answer if isinstance(answer, str) else json.dumps(answer)
    monkeypatch.setattr(om.llm, "complete",
                        lambda prompt, **kw: f"```json\n{raw}\n```")


def stub_app(monkeypatch, expenses):
    """Returns the list of PUTs the matcher made, so a test can assert on what
    would have been written to the ledger."""
    puts = []
    monkeypatch.setattr(om, "get_json", lambda path: {"expenses": expenses})
    monkeypatch.setattr(om, "put_json", lambda path, data: puts.append((path, data)))
    monkeypatch.setattr(om, "_categorize_batch",
                        lambda txs, key: [{**txs[0], "category": "Home: Small accessories"}])
    monkeypatch.setattr(om, "validate_category", lambda c: c)
    return puts


def expense(tx_id, amount, date="2026-03-06"):
    return {"id": tx_id, "amount": amount, "date": date}


# ─── Reading the email ────────────────────────────────────────────────────────

def test_model_answer_becomes_an_order(monkeypatch):
    stub_llm(monkeypatch, GOOD_ANSWER)
    order = om.extract_order("Amazon", "any email body", "key", "PLN")
    assert order["order_id"] == "302-1234567-1234567"
    assert order["total_paid"] == 49.99
    assert order["currency"] == "EUR"
    assert order["order_date"].date().isoformat() == "2026-03-05"
    assert [it["name"] for it in order["items"]] == ["USB-C cable 2 m", "Phone case"]


def test_amount_written_as_text_is_still_read(monkeypatch):
    stub_llm(monkeypatch, {**GOOD_ANSWER, "total": "1 234,56 zł"})
    order = om.extract_order("Amazon", "any email body", "key", "PLN")
    assert order["total_paid"] == 1234.56


def test_malformed_answers_are_rejected(monkeypatch):
    bad = [
        "not json at all",                                  # model wrote prose
        {"is_order": False},                                # a shipping notice
        {**GOOD_ANSWER, "total": None},                     # field missing
        {**GOOD_ANSWER, "total": "not an amount"},          # unparseable amount
        {**GOOD_ANSWER, "total": -49.99},                   # nonsense amount
        {**GOOD_ANSWER, "order_date": "05/03/2026"},        # its own date format
        {**GOOD_ANSWER, "order_date": "last Tuesday"},
        {**GOOD_ANSWER, "currency": "euros"},               # not an ISO code
        {**GOOD_ANSWER, "order_id": ""},                    # nothing to dedupe on
        {**GOOD_ANSWER, "items": []},                       # nothing to describe
        {"is_order": True, "invented_field": "surprise"},   # made it up
        ["not", "even", "an", "object"],
    ]
    for answer in bad:
        stub_llm(monkeypatch, answer)
        assert om.extract_order("Amazon", "body", "key", "PLN") is None, answer


# ─── Matching ─────────────────────────────────────────────────────────────────

def test_foreign_currency_matches_within_the_fx_band(monkeypatch):
    # 49.99 EUR × 4.30 = 214.96 PLN; the bank charged 217.42 at its own rate.
    puts = stub_app(monkeypatch, [expense(11, 217.42)])
    stub_llm(monkeypatch, GOOD_ANSWER)
    order = om.extract_order("Amazon", "body", "key", "PLN")
    state = {"matched": []}

    assert om.match_and_categorize("Amazon", order, CATEGORIES, "key", state, OPTS) is True
    assert puts[0][0] == "/api/transactions/11"
    assert puts[0][1]["category_id"] == 7
    # Both amounts on the row: the converted one is on the statement, the
    # original is what the shop charged.
    assert puts[0][1]["description"] == "Amazon - USB-C cable 2 m, Phone case (49.99 EUR)"
    assert state["matched"] == ["Amazon:302-1234567-1234567"]

    # Same order again on the next run changes nothing.
    assert om.match_and_categorize("Amazon", order, CATEGORIES, "key", state, OPTS) is False
    assert len(puts) == 1


def test_unknown_rate_leaves_the_order_alone(monkeypatch):
    puts = stub_app(monkeypatch, [expense(11, 217.42)])
    stub_llm(monkeypatch, {**GOOD_ANSWER, "currency": "USD"})
    order = om.extract_order("AliExpress", "body", "key", "PLN")
    state = {"matched": []}

    assert om.match_and_categorize("AliExpress", order, CATEGORIES, "key", state, OPTS) is False
    assert puts == [] and state["matched"] == []


def test_two_candidates_in_the_band_go_to_manual_review(monkeypatch):
    puts = stub_app(monkeypatch, [expense(11, 217.42), expense(12, 210.00)])
    stub_llm(monkeypatch, GOOD_ANSWER)
    order = om.extract_order("Amazon", "body", "key", "PLN")
    state = {"matched": []}

    assert om.match_and_categorize("Amazon", order, CATEGORIES, "key", state, OPTS) is False
    assert puts == [] and state["matched"] == []


def test_local_currency_still_needs_an_exact_amount(monkeypatch):
    puts = stub_app(monkeypatch, [expense(11, 50.50)])
    stub_llm(monkeypatch, {**GOOD_ANSWER, "currency": "PLN"})
    order = om.extract_order("Temu", "body", "key", "PLN")
    state = {"matched": []}

    assert om.match_and_categorize("Temu", order, CATEGORIES, "key", state, OPTS) is False
    assert puts == []


def test_a_transaction_weeks_away_is_not_the_same_purchase(monkeypatch):
    puts = stub_app(monkeypatch, [expense(11, 217.42, date="2026-04-20")])
    stub_llm(monkeypatch, GOOD_ANSWER)
    order = om.extract_order("Amazon", "body", "key", "PLN")

    assert om.match_and_categorize("Amazon", order, CATEGORIES, "key", {}, OPTS) is False
    assert puts == []


# ─── Configuration ────────────────────────────────────────────────────────────

def test_without_configuration_only_allegro_is_read():
    opts = om.options({})
    assert opts["shops"] == {"Allegro": {"folder": "Allegro"}}
    assert opts["fx_rates"] == {}
    assert opts["fx_tolerance_pct"] == om.DEFAULT_FX_TOLERANCE_PCT


def clear_mail_env(monkeypatch):
    """mailbox() falls back to the real environment, which a test must not see."""
    for v in ("MAIL_ADDRESS", "MAIL_PASSWORD", "MAIL_IMAP_HOST", "MAIL_IMAP_PORT",
              "GMAIL_ADDRESS", "GMAIL_APP_PASSWORD"):
        monkeypatch.delenv(v, raising=False)


def test_mailbox_defaults_to_gmail(monkeypatch):
    clear_mail_env(monkeypatch)
    mbox = om.mailbox({"MAIL_ADDRESS": "me@example.com", "MAIL_PASSWORD": "secret"})
    assert mbox["host"] == "imap.gmail.com" and mbox["port"] == 993


def test_mailbox_on_another_server(monkeypatch):
    clear_mail_env(monkeypatch)
    mbox = om.mailbox({"MAIL_ADDRESS": "me@example.com", "MAIL_PASSWORD": "secret",
                       "MAIL_IMAP_HOST": "imap.fastmail.com", "MAIL_IMAP_PORT": "993"})
    assert mbox["host"] == "imap.fastmail.com" and mbox["port"] == 993


def test_the_old_gmail_variable_names_still_work(monkeypatch):
    clear_mail_env(monkeypatch)
    mbox = om.mailbox({"GMAIL_ADDRESS": "me@gmail.com", "GMAIL_APP_PASSWORD": "app pw"})
    assert mbox["address"] == "me@gmail.com" and mbox["password"] == "app pw"
    assert mbox["host"] == "imap.gmail.com"


def test_no_credentials_means_no_mailbox(monkeypatch):
    clear_mail_env(monkeypatch)
    assert om.mailbox({"MAIL_ADDRESS": "me@example.com"}) is None
    assert om.mailbox({}) is None


def test_allegro_is_parsed_not_sent_to_the_model(monkeypatch):
    monkeypatch.setattr(om.llm, "complete",
                        lambda *a, **kw: pytest_fail("the Allegro path must not call the model"))
    text = ("z dnia 5 marca 2026, 14:30\nKabel USB-C 2 m\n49,00 zł\n"
            "RAZEM\n49,00 zł\nNumer płatności 00000000-0000-4000-8000-000000000000\n")
    order = om.extract_order("Allegro", text, "key", "PLN")
    assert order["total_paid"] == 49.0
    assert order["currency"] == "PLN"
    assert order["order_id"] == "00000000-0000-4000-8000-000000000000"


def test_a_price_above_a_thousand_is_read(monkeypatch):
    """Allegro separates thousands with a non-breaking space. PRICE_RE accepts it,
    so the raw "1\u00a0137,77" reached parse_amount, where .replace(" ", "") — which
    only strips U+0020 — left it unparseable. The ValueError escaped the loop in
    main() and took the rest of the run with it."""
    monkeypatch.setattr(om.llm, "complete",
                        lambda *a, **kw: pytest_fail("the Allegro path must not call the model"))
    text = ("z dnia 10 sierpnia 2026, 09:15\nLampa ogrodowa\n1\u00a0137,77 z\u0142\n"
            "Metoda dostawy\nRAZEM\n1\u00a0137,77 z\u0142\nNumer p\u0142atno\u015bci "
            "00000000-0000-4000-8000-000000000001\n")
    order = om.extract_order("Allegro", text, "key", "PLN")
    assert order["total_paid"] == 1137.77, order["total_paid"]
    assert order["items"] == [{"name": "Lampa ogrodowa", "price": 1137.77}], order["items"]


def test_one_unreadable_email_does_not_cost_the_whole_run(monkeypatch, tmp_path):
    """Every shop shares one loop, so an exception from a single email used to stop
    the shops queued behind it. Worse, the digest cache is written after the loop:
    escaping it discarded the digests of emails already read, and the next run paid
    the model to read them again."""
    state_file = tmp_path / "state.json"
    monkeypatch.setattr(om, "STATE_FILE", state_file)
    monkeypatch.setattr(om, "load_env", lambda: {})
    monkeypatch.setattr(om, "mailbox", lambda env: {"host": "imap.example.com"})
    monkeypatch.setattr(om, "get_api_key", lambda env: "key")
    monkeypatch.setattr(om, "load_config", lambda: {"order_matching": {
        "shops": {"Allegro": {"folder": "Allegro"}, "Amazon": {"folder": "Amazon"}}}})
    monkeypatch.setattr(om, "get_json", lambda path: CATEGORIES)
    monkeypatch.setattr(om, "fetch_shop_emails",
                        lambda mbox, shops: [("Allegro", "unreadable"), ("Amazon", "readable")])

    def extract(shop, text, api_key, currency):
        if text == "unreadable":
            raise ValueError("could not convert string to float: '1\u00a0137.77'")
        return {"total_paid": 49.99}
    monkeypatch.setattr(om, "extract_order", extract)

    read = []

    def matcher(shop, order, tree, key, state, opts):
        read.append(shop)
        return True
    monkeypatch.setattr(om, "match_and_categorize", matcher)

    om.main()

    assert read == ["Amazon"], f"the shop behind the bad email was skipped: {read}"
    assert len(json.loads(state_file.read_text())["done"]) == 1, \
        "the digest cache must survive the bad email, or the next run pays twice"


def pytest_fail(msg):
    raise AssertionError(msg)
