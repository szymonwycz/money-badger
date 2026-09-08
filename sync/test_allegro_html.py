#!/usr/bin/env python3
"""Self-check for the text/html fallback in extract_plaintext() — run directly,
no framework/fixtures.

python3 sync/test_allegro_html.py
"""
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from order_match import extract_plaintext, parse_allegro_email

# Minimal reproduction of a direct (non-forwarded) Allegro notification: only
# text/html, no text/plain — this is the case that used to be silently dropped.
# Deliberately keeps the two real-world quirks that broke a naive tag-stripper:
# a literal newline inside a <p> ("z dnia" / date on separate source lines,
# still meant to render as one line) and a price split across inline <span>s.
HTML = """
<html><body>
<p>z dnia
    5 marca 2026, 14:30</p>
<table><tr>
<td><a href="https://allegro.pl/x">Kabel USB-C 2 m</a><br>(1234567890)</td>
<td>49,<span>00 zł</span></td>
</tr></table>
<h3>Metoda dostawy</h3>
<div>Kurier</div>
<h3>RAZEM</h3>
<div>49,<span>00 zł</span></div>
<h3>Numer płatności</h3>
<p>00000000-0000-4000-8000-000000000000</p>
</body></html>
"""


def html_only_message():
    msg = MIMEMultipart("mixed")
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(HTML, "html", "utf-8"))
    msg.attach(alt)
    return msg


def test_extract_plaintext_falls_back_to_html():
    text = extract_plaintext(html_only_message())
    assert text is not None, "expected html fallback text, got None"
    assert "z dnia 5 marca 2026, 14:30" in text, text
    assert "49,00 zł" in text, text


def test_parse_allegro_email_reads_html_fallback_text():
    text = extract_plaintext(html_only_message())
    order = parse_allegro_email(text)
    assert order["order_date"] is not None, "order_date not parsed"
    assert order["order_date"].isoformat() == "2026-03-05T14:30:00"
    assert order["total_paid"] == 49.0, order["total_paid"]
    assert order["payment_id"] == "00000000-0000-4000-8000-000000000000"
    assert len(order["items"]) == 1
    assert order["items"][0]["price"] == 49.0


# A trimmed but faithful copy of a real two-seller order email (2026-09-03,
# 115.49 charged): no "RAZEM" line, products split across two blocks separated
# by "Metoda dostawy", and the card amount only after "Płatność" — lower than
# the order total, because a coupon came off.
MULTI_SELLER = (
    "kupiłaś 2 produkty, a sprzedający otrzymali Twoją wpłatę.\n"
    "z dnia 3 września 2026, 14:36\n"
    "od SellerOne\n"
    "Proszek do prania kolorów Persil 10 kg\n"
    "(18872319658)\n"
    "74,99\u00a0zł\n"
    "Metoda dostawy\n"
    "Kurier, DHL\n"
    "0,00\u00a0zł\n"
    "z pakietem\n"
    "14,99\u00a0zł\n"
    "74,99\u00a0zł\n"
    "89,98\u00a0zł\n"
    "od SellerTwo\n"
    "Lovela Baby Proszek hipoalergiczny 4,1kg\n"
    "(17603171517)\n"
    "50,50\u00a0zł\n"
    "Metoda dostawy\n"
    "Kurier, Allegro One\n"
    "0,00\u00a0zł\n"
    "z pakietem\n"
    "14,99\u00a0zł\n"
    "50,50\u00a0zł\n"
    "65,49\u00a0zł\n"
    "Kwota za zakupy\n"
    "Razem z dostawą\n"
    "125,49\u00a0zł\n"
    "155,47\u00a0zł\n"
    "Kupon o wartości 10,00 zł\n"
    "-10,00\u00a0zł\n"
    "115,49\u00a0zł\n"
    "155,47\u00a0zł\n"
    "Płatność\n"
    "115,49\u00a0zł\n"
    "Numer płatności 191bb282-a794-11f1-a870-731c4a78d83e\n"
)


def test_multi_seller_order_is_parsed_whole():
    order = parse_allegro_email(MULTI_SELLER)
    # The card amount, not the order total — anything else misses the statement.
    assert order["total_paid"] == 115.49, order["total_paid"]
    names = [i["name"] for i in order["items"]]
    assert len(names) == 2, names
    assert names[0].startswith("Proszek do prania"), names
    assert names[1].startswith("Lovela Baby"), names
    assert order["order_date"].isoformat() == "2026-09-03T14:36:00"
    assert order["payment_id"] == "191bb282-a794-11f1-a870-731c4a78d83e"


def test_delivery_lines_never_become_items():
    """Shipping costs and "z pakietem" sit between the product blocks — without
    the skip they would land on the item list as products."""
    order = parse_allegro_email(MULTI_SELLER)
    assert not any("pakietem" in i["name"] or "Kurier" in i["name"]
                   for i in order["items"]), order["items"]


def test_text_plain_part_still_wins_when_present():
    msg = html_only_message()
    msg.attach(MIMEText("plain body wins", "plain", "utf-8"))
    assert extract_plaintext(msg) == "plain body wins"


if __name__ == "__main__":
    test_extract_plaintext_falls_back_to_html()
    test_parse_allegro_email_reads_html_fallback_text()
    test_text_plain_part_still_wins_when_present()
    test_multi_seller_order_is_parsed_whole()
    test_delivery_lines_never_become_items()
    print("OK — all html fallback tests passed")
