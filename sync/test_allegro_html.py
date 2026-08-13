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
from allegro_match import extract_plaintext, parse_allegro_email

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


def test_text_plain_part_still_wins_when_present():
    msg = html_only_message()
    msg.attach(MIMEText("plain body wins", "plain", "utf-8"))
    assert extract_plaintext(msg) == "plain body wins"


if __name__ == "__main__":
    test_extract_plaintext_falls_back_to_html()
    test_parse_allegro_email_reads_html_fallback_text()
    test_text_plain_part_still_wins_when_present()
    print("OK — wszystkie testy html fallback przeszły")
