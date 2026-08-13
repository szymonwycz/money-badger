#!/usr/bin/env python3
"""
order_match.py — reads order confirmation emails out of a mailbox, matches them
against CHECK ME transactions by amount + date, and categorizes them through the
existing LLM pipeline from main.py (the same _categorize_batch, so the prompt
and the category list are not duplicated).

The card statement says only "ALLEGRO" and an amount — the email says what was
actually bought.

There are two ways to read an email:

  * a parser, for a shop whose template someone has actually seen. Allegro has
    one (parse_allegro_email): exact, instant and free.
  * the model, for every other shop. The whole email is handed to the LLM and
    comes back as a structured order. Slower and not free, but it needs no
    knowledge of the template and survives the shop redesigning it.

Writing a fast parser for a shop later is one function plus one line in PARSERS;
nothing else changes.

Which shops are read, and from which mail folder, comes from sync/config.json:

    "order_matching": {
      "local_currency": "PLN",
      "fx_rates": {"EUR": 4.30, "USD": 4.00},
      "fx_tolerance_pct": 5,
      "shops": {
        "Allegro": {"folder": "Allegro"},
        "Amazon":  {"folder": "Amazon"}
      }
    }

Without that block only Allegro is read, from the "Allegro" folder — exactly what
this did before shops were configurable.

Foreign currencies: an Amazon.de order in EUR reaches the statement in the
account currency, converted at the bank's own rate, so an exact amount match is
impossible. A rate from fx_rates converts the order and a candidate counts when
it lands within fx_tolerance_pct (5% by default) of that, which is wide enough
for the bank's spread and still narrow enough not to swallow a neighbouring
payment. When no rate is configured for the currency nothing is guessed: the
order is reported and left for manual review.

Requires in .env:
  MAIL_ADDRESS=<the mailbox the confirmations arrive at>
  MAIL_PASSWORD=<its password, an app password wherever the provider has them>
  MAIL_IMAP_HOST=<optional, defaults to Gmail's>
  MAIL_IMAP_PORT=<optional, defaults to 993>

If the address or the password is missing the step is skipped without an error,
like the other optional steps in master_pi.py.
"""
import email
import hashlib
import imaplib
import json
import os
import re
import sys
from html.parser import HTMLParser
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))
import llm  # noqa: E402
from main import (load_env, load_config, get_api_key, _categorize_batch,  # noqa: E402
                  validate_category)
sys.path.insert(0, str(BASE_DIR.parent))
import budget_client  # noqa: E402
from logging_setup import start_logging  # noqa: E402

BUDGET_URL  = budget_client.BUDGET_URL
STATE_FILE  = BASE_DIR / ".allegro_processed.json"
DATE_TOLERANCE_DAYS = 5
AMOUNT_TOLERANCE = 0.02
# Wide enough for a bank's exchange spread on a converted card payment.
DEFAULT_FX_TOLERANCE_PCT = 5.0
# Nothing useful for us lives past this in an order email, and the model is paid
# by the token.
MAX_EMAIL_CHARS = 12000
DEFAULT_SHOPS = {"Allegro": {"folder": "Allegro"}}
DEFAULT_IMAP_HOST = "imap.gmail.com"
DEFAULT_IMAP_PORT = 993

MONTHS = {
    "stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5, "czerwca": 6,
    "lipca": 7, "sierpnia": 8, "września": 9, "października": 10,
    "listopada": 11, "grudnia": 12,
}
PRICE_RE      = re.compile(r"^(\d[\d\s]*,\d{2})\s*zł$")
URL_RE        = re.compile(r"^<https?://.*>$")
OFFER_ID_RE   = re.compile(r"^\(\d+\)$")
DATE_RE       = re.compile(r"z dnia (\d{1,2}) (\w+) (\d{4}),\s*(\d{2}):(\d{2})")
PAYMENT_ID_RE = re.compile(r"Numer płatności\s+([0-9a-f-]{36})", re.IGNORECASE)


def parse_amount(s: str) -> float:
    return float(s.replace(" ", "").replace(",", "."))


def parse_allegro_email(text: str) -> dict:
    """Pulls the order date, the products with their prices and the total paid
    out of the message body (plain text). Works whether the email is a direct
    Allegro notification or a manually forwarded "Fwd:" — both carry the same
    original text inside."""
    m = DATE_RE.search(text)
    order_date = None
    if m:
        day, month_name, year, hh, mm = m.groups()
        month = MONTHS.get(month_name.lower())
        if month:
            order_date = datetime(int(year), month, int(day), int(hh), int(mm))

    # Drop the <...> links and the offer ids, which leaves clean
    # [product name, price] pairs next to each other.
    lines = [l.strip() for l in text.splitlines()]
    clean = [l for l in lines if l and not URL_RE.match(l) and not OFFER_ID_RE.match(l)]

    items, pending_name = [], None
    for l in clean:
        if l.startswith("Metoda dostawy"):
            break
        pm = PRICE_RE.match(l)
        if pm:
            if pending_name:
                items.append({"name": pending_name, "price": parse_amount(pm.group(1))})
                pending_name = None
        else:
            pending_name = l

    razem_idx = next((i for i, l in enumerate(clean) if l == "RAZEM"), None)
    total_paid = None
    if razem_idx is not None and razem_idx + 1 < len(clean):
        pm = PRICE_RE.match(clean[razem_idx + 1])
        if pm:
            total_paid = parse_amount(pm.group(1))

    pid_m = PAYMENT_ID_RE.search(text)
    payment_id = pid_m.group(1) if pid_m else None

    return {
        "order_date": order_date,
        "items": items,
        "total_paid": total_paid,
        "payment_id": payment_id,
    }


class _HTMLTextExtractor(HTMLParser):
    """Turns HTML into text, breaking lines at block tags — close enough to the
    layout of Allegro's text/plain mail that parse_allegro_email() reads it
    unchanged (product name and price on separate lines, "RAZEM" and so on)."""

    _BLOCK_TAGS = {"p", "div", "tr", "td", "th", "li", "table",
                   "h1", "h2", "h3", "h4", "h5", "h6"}
    _SKIP_TAGS = {"script", "style"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
        elif tag == "br" or tag in self._BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_startendtag(self, tag, attrs):
        if tag == "br":
            self._chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in self._BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data):
        if not self._skip_depth:
            # Collapse literal \n/\t coming from the HTML source formatting into
            # spaces — lines must come only from our own block-tag markers, not
            # from the sender's pretty-printing.
            self._chunks.append(re.sub(r'[\r\n\t]+', ' ', data))

    def text(self) -> str:
        lines = ("".join(self._chunks)).splitlines()
        lines = [re.sub(r"[ \t]+", " ", l).strip() for l in lines]
        return "\n".join(l for l in lines if l)


def html_to_text(html: str) -> str:
    parser = _HTMLTextExtractor()
    parser.feed(html)
    return parser.text()


def extract_plaintext(msg) -> str | None:
    """text/plain is preferred; when the email has none (a direct shop
    notification rather than a forwarded Fwd:), text/html is flattened into
    text — otherwise such an email would be silently skipped."""
    if msg.is_multipart():
        html_fallback = None
        for part in msg.walk():
            disp = str(part.get("Content-Disposition", ""))
            if "attachment" in disp:
                continue
            if part.get_content_type() == "text/plain":
                charset = part.get_content_charset() or "utf-8"
                payload = part.get_payload(decode=True)
                return payload.decode(charset, errors="replace") if payload else None
            if part.get_content_type() == "text/html" and html_fallback is None:
                charset = part.get_content_charset() or "utf-8"
                payload = part.get_payload(decode=True)
                if payload:
                    html_fallback = html_to_text(payload.decode(charset, errors="replace"))
        return html_fallback
    charset = msg.get_content_charset() or "utf-8"
    payload = msg.get_payload(decode=True)
    return payload.decode(charset, errors="replace") if payload else None


# ─── Reading an order out of an email ─────────────────────────────────────────

LLM_PROMPT = """You are reading one email and deciding whether it confirms a
purchase the buyer has paid for, and if so, what was bought.

Answer with JSON only, no prose, in exactly this shape:

{"is_order": true, "order_id": "302-1234567-1234567", "order_date": "2026-03-05",
 "total": 49.99, "currency": "EUR", "items": ["USB-C cable 2 m", "Phone case"]}

Rules:
- is_order is false for anything that is not a paid order: shipping and delivery
  notices, refunds, cancellations, payment reminders, wish lists, marketing.
  Then answer {"is_order": false} and nothing else.
- total is the amount actually charged, as a number, with a dot for the decimal
  separator, no currency symbol and no thousands separator.
- currency is the ISO code of that amount: EUR, USD, PLN, GBP.
- order_date is the date the order was placed, as YYYY-MM-DD.
- order_id is the shop's own order number.
- items are the product names, without prices or quantities.
- Leave out any field the email does not state. Never invent one.

The email follows.

---
"""


def _to_float(v):
    """Models return a number most of the time and '49,99' or '1 234,56 zł' the
    rest of the time. Anything that is not a number after that is not a number:
    return None and let the caller drop the order."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if not isinstance(v, str):
        return None
    s = re.sub(r"[^\d,.\-]", "", v)
    if "," in s and "." in s:
        # Whichever separator comes last is the decimal one.
        s = s.replace(",", "") if s.rfind(".") > s.rfind(",") else s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def clean_llm_order(data) -> dict | None:
    """Whatever the model answered, into the shape a parser produces — or None.

    A model can return the amount as text, a date in a format of its own, a
    currency called "euros" or a field nobody asked for. This is a money path,
    so anything unexpected drops the order to manual review rather than being
    repaired by guesswork."""
    if not isinstance(data, dict) or not data.get("is_order"):
        return None

    order_id = str(data.get("order_id") or "").strip()[:64]
    currency = str(data.get("currency") or "").strip().upper()
    total = _to_float(data.get("total"))
    if not order_id or not total or total <= 0 or not re.fullmatch(r"[A-Z]{3}", currency):
        return None

    try:
        order_date = datetime.fromisoformat(str(data.get("order_date")))
    except (TypeError, ValueError):
        return None

    items = []
    for it in (data.get("items") or [])[:20]:
        name = it.get("name") if isinstance(it, dict) else it
        if isinstance(name, str) and name.strip():
            items.append({"name": name.strip()[:120]})
    if not items:
        return None

    return {"order_id": order_id, "order_date": order_date, "items": items,
            "total_paid": total, "currency": currency}


def llm_extract_order(shop: str, text: str, api_key: str) -> dict | None:
    raw = llm.complete(LLM_PROMPT + text[:MAX_EMAIL_CHARS], task="categorize",
                       max_tokens=1000, api_key=api_key)
    try:
        return clean_llm_order(llm.extract_json(raw))
    except (ValueError, TypeError):
        print(f"  [{shop.lower()}] the model did not answer with JSON — skipping this email")
        return None


# Shops whose template we have seen and parse directly. Everything else goes to
# the model. Adding a shop here is one function above plus one line here.
PARSERS = {"Allegro": parse_allegro_email}


def extract_order(shop: str, text: str, api_key: str, local_currency: str) -> dict | None:
    parser = PARSERS.get(shop)
    if not parser:
        return llm_extract_order(shop, text, api_key)
    order = parser(text)
    # A parser is written for one shop and already knows the currency it prints;
    # only the model has to report one.
    return {**order, "order_id": order.get("payment_id"), "currency": local_currency}


# ─── Mail ─────────────────────────────────────────────────────────────────────

def mailbox(env: dict) -> dict | None:
    """Where mail is read from, or None when it isn't configured.

    Any IMAP server over TLS; the defaults are Gmail's, which is where this
    started. GMAIL_ADDRESS/GMAIL_APP_PASSWORD are the names the first version
    used and still work, so an existing .env needs no editing."""
    def val(*names, default=None):
        for n in names:
            v = env.get(n) or os.environ.get(n)
            if v:
                return v
        return default

    address  = val("MAIL_ADDRESS", "GMAIL_ADDRESS")
    password = val("MAIL_PASSWORD", "GMAIL_APP_PASSWORD")
    if not address or not password:
        return None
    port = val("MAIL_IMAP_PORT", default="")
    return {
        "address":  address,
        "password": password,
        "host":     val("MAIL_IMAP_HOST", default=DEFAULT_IMAP_HOST),
        "port":     int(port) if str(port).isdigit() else DEFAULT_IMAP_PORT,
    }


def fetch_shop_emails(mbox: dict, shops: dict) -> list[tuple[str, str]]:
    """IMAP: every email in each shop's folder from the last 30 days, as
    (shop, text) pairs.

    Mail lands in that folder because of a server-side rule you write yourself
    (see README) — on Gmail a label is a folder, so a filter that labels is all
    the setup there is. Sorting server-side means nothing here has to parse
    subjects or senders, and mail forwarded from a second account is picked up
    too. The folder is opened directly rather than INBOX: a Gmail filter that
    labels usually archives as well, so searching INBOX missed exactly those."""
    imap = imaplib.IMAP4_SSL(mbox["host"], mbox["port"])
    imap.login(mbox["address"], mbox["password"])
    since = (datetime.now() - timedelta(days=30)).strftime("%d-%b-%Y")
    out = []
    for shop, shop_cfg in shops.items():
        folder = (shop_cfg or {}).get("folder", shop)
        try:
            typ, _ = imap.select(f'"{folder}"')
        except imaplib.IMAP4.error:
            typ = "NO"
        if typ != "OK":
            # A shop enabled before its mail rule exists must not take the other
            # shops down with it.
            print(f"  [{shop.lower()}] no mail folder \"{folder}\" — skipping this shop")
            continue
        typ, data = imap.search(None, "SINCE", since)
        if typ != "OK":
            continue
        found = 0
        for eid in data[0].split():
            typ, msg_data = imap.fetch(eid, "(RFC822)")
            if typ != "OK" or not msg_data or not msg_data[0]:
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            text = extract_plaintext(msg)
            if text:
                out.append((shop, text))
                found += 1
        print(f"  [{shop.lower()}] {found} emails in folder \"{folder}\".")
    imap.logout()
    return out


def get_json(path: str):
    return budget_client.get(path)


def put_json(path: str, data: dict):
    return budget_client.put(path, data, timeout=10)


def find_category_id(tree: list, dotted_name: str):
    """'Home: Small accessories' -> id, from the /api/categories tree."""
    if ": " in dotted_name:
        parent_name, child_name = dotted_name.split(": ", 1)
        for g in tree:
            if g["name"] == parent_name:
                for c in g["children"]:
                    if c["name"] == child_name:
                        return c["id"]
        return None
    for g in tree:
        if g["name"] == dotted_name:
            return g["id"]
    return None


# ─── Matching ─────────────────────────────────────────────────────────────────

def options(cfg: dict) -> dict:
    om = cfg.get("order_matching") or {}
    return {
        "local_currency":   (om.get("local_currency") or "PLN").upper(),
        "fx_rates":         {str(k).upper(): v for k, v in (om.get("fx_rates") or {}).items()},
        "fx_tolerance_pct": float(om.get("fx_tolerance_pct") or DEFAULT_FX_TOLERANCE_PCT),
        "shops":            om.get("shops") or DEFAULT_SHOPS,
    }


def to_local_amount(order: dict, opts: dict) -> tuple:
    """(amount to look for on the statement, tolerance around it) — or
    (None, None) when the order is in another currency and no rate is configured
    for it. The bank converts at its own rate, so a foreign order can only ever
    be matched within a band, never to the cent."""
    if order["currency"] == opts["local_currency"]:
        return order["total_paid"], AMOUNT_TOLERANCE
    rate = opts["fx_rates"].get(order["currency"])
    if not rate:
        return None, None
    expected = order["total_paid"] * float(rate)
    return expected, expected * opts["fx_tolerance_pct"] / 100


def match_and_categorize(shop: str, order: dict, categories_tree: list,
                         api_key: str, state: dict, opts: dict) -> bool:
    """True when the order was matched to a transaction and written back."""
    tag = shop.lower()
    oid = order.get("order_id")
    # Allegro keeps the bare payment id as its key, so state written before other
    # shops existed still counts as processed.
    key = oid if shop == "Allegro" else f"{shop}:{oid}"
    if not oid or key in state.get("matched", []):
        return False
    if not order.get("total_paid") or not order.get("order_date") or not order.get("items"):
        return False

    expected, tolerance = to_local_amount(order, opts)
    if expected is None:
        print(f"  [{tag}] {oid}: {order['total_paid']:.2f} {order['currency']} — no rate for "
              f"{order['currency']} in order_matching.fx_rates, leaving for manual review")
        return False

    unreviewed = get_json("/api/transactions/unreviewed")
    order_date = order["order_date"].date()
    candidates = [
        tx for tx in unreviewed.get("expenses", [])
        if abs(tx["amount"] - expected) < tolerance
        and abs((datetime.strptime(tx["date"], "%Y-%m-%d").date() - order_date).days) <= DATE_TOLERANCE_DAYS
    ]

    if len(candidates) != 1:
        print(f"  [{tag}] {oid}: {len(candidates)} candidates for {expected:.2f} "
              f"{opts['local_currency']} around {order_date} — skipping (manual review)")
        return False

    tx = candidates[0]
    item_names = ", ".join(it["name"] for it in order["items"])
    description = f"{shop} - {item_names}"
    if order["currency"] != opts["local_currency"]:
        # The statement shows the converted amount; the original belongs in the
        # description, where a human checking the match can see both.
        description += f" ({order['total_paid']:.2f} {order['currency']})"
    fake_tx = {
        "amount": -tx["amount"],
        "transaction_date": order_date.isoformat(),
        "title": item_names,
        "counterpart": shop,
        "_transfer": False,
    }
    categorized = _categorize_batch([fake_tx], api_key)
    category = validate_category(categorized[0]["category"])
    cat_id = find_category_id(categories_tree, category)

    # The description (product names) is always written — it makes manual review
    # and correction in CHECK ME easier whether or not the model got the category
    # right. A category corrected in the UI feeds the existing "corrections"
    # pipeline → main.py learn.
    update = {"description": description}
    if cat_id is not None:
        update["category_id"] = cat_id
    put_json(f"/api/transactions/{tx['id']}", update)
    state.setdefault("matched", []).append(key)

    if cat_id is not None:
        print(f"  [{tag}] {oid}: {item_names[:60]} → {category} ({tx['amount']:.2f} {opts['local_currency']})")
    else:
        print(f"  [{tag}] {oid}: {item_names[:60]} → ambiguous category "
              f"('{category}'), description saved, stays in CHECK ME")
    return True


def main():
    env = load_env()
    mbox = mailbox(env)
    if not mbox:
        print("  [orders] MAIL_ADDRESS/MAIL_PASSWORD not configured — skipping.")
        return

    api_key = get_api_key(env)
    if not (api_key or llm.configured()):
        print("  [orders] no LLM configured — skipping.")
        return

    opts = options(load_config())
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {"matched": []}
    done = state.setdefault("done", [])

    try:
        categories_tree = get_json("/api/categories")
    except (urllib.error.URLError, OSError) as e:
        print(f"  [orders] app unreachable: {e}")
        return

    try:
        emails = fetch_shop_emails(mbox, opts["shops"])
    except (imaplib.IMAP4.error, OSError) as e:
        print(f"  [orders] IMAP error: {e}")
        return

    for shop, text in emails:
        # An email already matched is never read again: with the model doing the
        # reading, re-reading a 30-day window every run costs real money. An
        # email that did not match is retried, because its transaction may only
        # show up on the statement a day or two later.
        digest = hashlib.md5(text.encode("utf-8", "replace")).hexdigest()
        if digest in done:
            continue
        order = extract_order(shop, text, api_key, opts["local_currency"])
        if order and match_and_categorize(shop, order, categories_tree, api_key, state, opts):
            done.append(digest)

    STATE_FILE.write_text(json.dumps(state, indent=2))


if __name__ == "__main__":
    # The log keeps the old name after the file was renamed: self_checker.py
    # watches logs/allegro_match_<date>.log, and the history under that name is
    # worth more than the tidiness of matching this module.
    log_path = start_logging("allegro_match", BASE_DIR.parent)
    print(f"(log: {log_path})")
    main()
