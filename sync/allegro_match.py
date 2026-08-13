#!/usr/bin/env python3
"""
allegro_match.py — czyta maile z potwierdzeniami zakupów Allegro (etykieta
"Allegro" w Gmailu), dopasowuje je do transakcji CHECK ME po kwocie+dacie,
i kategoryzuje przez istniejący pipeline LLM z main.py (ten sam
_categorize_batch, żeby nie duplikować promptu/list kategorii).

Karta pokazuje tylko "ALLEGRO" i kwotę — mail mówi co faktycznie kupiono.
Polski sklep, ale kod jest przyzwoitym szablonem dla dowolnego
paragonu-w-mailu: parser jest jedną funkcją, reszta to dopasowanie.

Wymaga w .env:
  GMAIL_ADDRESS=<twój adres>
  GMAIL_APP_PASSWORD=<hasło aplikacji Gmail, wymaga weryfikacji dwuetapowej>

Jeśli GMAIL_ADDRESS/GMAIL_APP_PASSWORD nie są ustawione, krok jest pomijany
bez błędu — tak samo jak inne opcjonalne kroki w master_pi.py.
"""
import email
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
from main import load_env, get_api_key, _categorize_batch, validate_category  # noqa: E402
sys.path.insert(0, str(BASE_DIR.parent))
import budget_client  # noqa: E402
from logging_setup import start_logging  # noqa: E402

BUDGET_URL  = budget_client.BUDGET_URL
STATE_FILE  = BASE_DIR / ".allegro_processed.json"
DATE_TOLERANCE_DAYS = 5
AMOUNT_TOLERANCE = 0.02

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
    """Wyciąga datę zamówienia, listę produktów z cenami, i sumę zapłaconą
    z treści maila (tekst zwykły). Odporne na to, czy mail jest bezpośrednim
    powiadomieniem Allegro, czy ręcznie przekazanym "Fwd:" — obie formy
    zawierają ten sam oryginalny tekst wewnątrz."""
    m = DATE_RE.search(text)
    order_date = None
    if m:
        day, month_name, year, hh, mm = m.groups()
        month = MONTHS.get(month_name.lower())
        if month:
            order_date = datetime(int(year), month, int(day), int(hh), int(mm))

    # Usuń linki <...> i numery ofert (id), zostawiając czyste pary
    # [nazwa produktu, cena] obok siebie.
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
    """Zamienia HTML na tekst z podziałem na linie przy tagach blokowych — na tyle
    zbliżony do layoutu maili text/plain z Allegro, że parse_allegro_email() go
    sparsuje bez zmian (nazwa produktu i cena na osobnych liniach, "RAZEM" itd.)."""

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
            # Zwiń literalne \n/\t z formatowania źródła HTML do spacji —
            # linie mają powstawać tylko z naszych własnych znaczników tagów
            # blokowych, nie z pretty-printingu maila.
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
    """Wolimy text/plain; jeśli mail go nie ma (bezpośrednie powiadomienie
    Allegro, bez pośrednictwa przekazanego Fwd:), spłaszczamy text/html do
    tekstu — inaczej taki mail byłby po cichu pomijany."""
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


def fetch_allegro_emails(address: str, app_password: str) -> list[str]:
    """Gmail IMAP: wszystkie maile z etykietą "Allegro" z ostatnich 30 dni.
    Etykietę nadaje filtr Gmaila, który konfigurujesz sam (patrz README) — dzięki
    temu nie trzeba parsować tematu/nadawcy i można wciągnąć też maile
    przekazywane z drugiego konta. Wybieramy folder etykiety bezpośrednio zamiast
    INBOX: filtr Gmaila częściowo archiwizuje te maile razem z nadaniem etykiety,
    więc szukanie w samym INBOX je pomijało."""
    imap = imaplib.IMAP4_SSL("imap.gmail.com")
    imap.login(address, app_password)
    imap.select('"Allegro"')
    since = (datetime.now() - timedelta(days=30)).strftime("%d-%b-%Y")
    typ, data = imap.search(None, "SINCE", since)
    texts = []
    if typ == "OK":
        for eid in data[0].split():
            typ, msg_data = imap.fetch(eid, "(RFC822)")
            if typ != "OK" or not msg_data or not msg_data[0]:
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            text = extract_plaintext(msg)
            if text:
                texts.append(text)
    imap.logout()
    return texts


def get_json(path: str):
    return budget_client.get(path)


def put_json(path: str, data: dict):
    return budget_client.put(path, data, timeout=10)


def find_category_id(tree: list, dotted_name: str):
    """'Home: Small accessories' -> id, z drzewa /api/categories."""
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


def match_and_categorize(order: dict, categories_tree: list, api_key: str, state: dict) -> None:
    pid = order["payment_id"]
    if not pid or pid in state.get("matched", []):
        return
    if not order["total_paid"] or not order["order_date"] or not order["items"]:
        return

    unreviewed = get_json("/api/transactions/unreviewed")
    order_date = order["order_date"].date()
    candidates = [
        tx for tx in unreviewed.get("expenses", [])
        if abs(tx["amount"] - order["total_paid"]) < AMOUNT_TOLERANCE
        and abs((datetime.strptime(tx["date"], "%Y-%m-%d").date() - order_date).days) <= DATE_TOLERANCE_DAYS
    ]

    if len(candidates) != 1:
        print(f"  [allegro] {pid[:8]}...: {len(candidates)} kandydatów dla "
              f"{order['total_paid']} zł z {order_date} — pomijam (ręczna weryfikacja)")
        return

    tx = candidates[0]
    item_names = ", ".join(it["name"] for it in order["items"])
    description = f"Allegro - {item_names}"
    fake_tx = {
        "amount": -order["total_paid"],
        "transaction_date": order_date.isoformat(),
        "title": item_names,
        "counterpart": "Allegro",
        "_transfer": False,
    }
    categorized = _categorize_batch([fake_tx], api_key)
    category = validate_category(categorized[0]["category"])
    cat_id = find_category_id(categories_tree, category)

    # Opis (nazwy produktów) zapisujemy zawsze — ułatwia ręczną weryfikację
    # i poprawianie w CHECK ME, niezależnie od tego czy Haiku trafił z kategorią.
    # Poprawka kategorii przez UI zasili istniejący pipeline "corrections" →
    # main.py learn.
    update = {"description": description}
    if cat_id is not None:
        update["category_id"] = cat_id
    put_json(f"/api/transactions/{tx['id']}", update)
    state.setdefault("matched", []).append(pid)

    if cat_id is not None:
        print(f"  [allegro] {pid[:8]}...: {item_names[:60]} → {category} ({order['total_paid']} zł)")
    else:
        print(f"  [allegro] {pid[:8]}...: {item_names[:60]} → kategoria niejednoznaczna "
              f"('{category}'), opis zapisany, zostaje w CHECK ME")


def main():
    env = load_env()
    address      = env.get("GMAIL_ADDRESS") or os.environ.get("GMAIL_ADDRESS")
    app_password = env.get("GMAIL_APP_PASSWORD") or os.environ.get("GMAIL_APP_PASSWORD")
    if not address or not app_password:
        print("  [allegro] GMAIL_ADDRESS/GMAIL_APP_PASSWORD nieskonfigurowane — pomijam.")
        return

    api_key = get_api_key(env)
    if not api_key:
        print("  [allegro] Brak ANTHROPIC_API_KEY — pomijam.")
        return

    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {"matched": []}

    try:
        categories_tree = get_json("/api/categories")
    except (urllib.error.URLError, OSError) as e:
        print(f"  [allegro] aplikacja niedostępna: {e}")
        return

    try:
        raw_emails = fetch_allegro_emails(address, app_password)
    except (imaplib.IMAP4.error, OSError) as e:
        print(f"  [allegro] Błąd IMAP: {e}")
        return

    print(f"  [allegro] Znaleziono {len(raw_emails)} maili z etykietą Allegro.")
    for text in raw_emails:
        order = parse_allegro_email(text)
        match_and_categorize(order, categories_tree, api_key, state)

    STATE_FILE.write_text(json.dumps(state, indent=2))


if __name__ == "__main__":
    log_path = start_logging("allegro_match", BASE_DIR.parent)
    print(f"(log: {log_path})")
    main()
