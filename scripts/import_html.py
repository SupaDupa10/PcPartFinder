"""Wandelt eine selbst gespeicherte Suchseite in das Importformat der App um.

Warum so und nicht per Scraper: Kleinanzeigen und eBay untersagen automatisiertes
Auslesen und sperren es technisch. Dieses Werkzeug stellt **keine** Anfragen an
eine Plattform. Es liest ausschliesslich eine HTML-Datei, die du selbst in
deinem Browser gespeichert hast (Rechtsklick -> "Seite speichern unter" oder
Strg+S), und erzeugt daraus die JSON-Datei, die der lokale Import-Connector der
App einliest. Damit landen echte Angebote in der App, ohne dass die Anwendung
eine fremde Seite abruft.

Aufruf::

    python scripts/import_html.py seite.html -o meine_angebote.json
    python scripts/import_html.py gespeicherte_seiten/*.html -o meine_angebote.json
    python scripts/import_html.py seite.html -o meine_angebote.json --merge

Erkennung (in dieser Reihenfolge):

1. ``application/ld+json`` (schema.org ItemList/Product/Offer) - die
   zuverlaessigste Quelle, weil Suchmaschinen diese Daten ebenfalls nutzen.
2. Links auf Anzeigen (Standard: ``s-anzeige``) mit Preis in der Umgebung.

Mit ``--debug`` wird ausgegeben, welche Quelle gegriffen hat. Sieht deine
gespeicherte Seite anders aus, ist die Erkennung in wenigen Zeilen anpassbar
(``--link-pattern``, ``LISTING_LINK_HINTS``).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

# Linkmuster je Plattform. eBay nutzt /itm/<id>, Kleinanzeigen /s-anzeige/.
DEFAULT_LINK_PATTERNS = ("s-anzeige", "s-anzeigen", "/itm/")
# Betraege in deutscher Schreibweise. Ein Leerzeichen darf NICHT als
# Tausendertrenner gelten: Sonst liest der Ausdruck quer ueber Wortgrenzen
# hinweg ("RTX 3080 268,50 €" wurde so zu 80268,50 €).
_AMOUNT = r"\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?|\d+(?:,\d{1,2})?"
# Preisangaben stehen je nach Seite vor oder nach dem Betrag: "215,00 €" bzw. "EUR 215,00".
PRICE_RE = re.compile(
    rf"(?<![\d.,])(?:(?P<value1>{_AMOUNT})\s*(?:€|EUR)|(?:€|EUR)\s*(?P<value2>{_AMOUNT}))(?!\d)"
)
POSTAL_RE = re.compile(r"\b(?P<postal>\d{5})\b\s*(?P<city>[A-ZÄÖÜ][\wÄÖÜäöüß.\- ]{2,30})")
# eBay nennt den Ort oft ohne Postleitzahl: "aus München". Bewusst nur ein
# Wort: Sonst wandern Begriffe wie "München Gebraucht" in die Ortsangabe.
CITY_ONLY_RE = re.compile(r"\baus\s+(?P<city>[A-ZÄÖÜ][\wÄÖÜäöüß.\-]{2,})")
DATE_RE = re.compile(r"\b(?P<day>\d{1,2})\.(?P<month>\d{1,2})\.(?P<year>\d{4})\b")
CONTEXT_WINDOW = 400


def to_float(raw: str) -> float | None:
    """Deutsche Schreibweise tolerant in eine Zahl umwandeln."""
    cleaned = raw.replace(" ", "").replace("\u00a0", "")
    if "," in cleaned and "." in cleaned:
        # 1.234,56 -> 1234.56
        cleaned = cleaned.replace(".", "").replace(",", ".")
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


class _PageParser(HTMLParser):
    """Sammelt sichtbaren Text, Links und JSON-LD-Blöcke einer HTML-Datei."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.chunks: list[str] = []
        self.anchors: list[dict] = []
        self.json_ld: list[str] = []
        self._text_len = 0
        self._stack: list[dict] = []
        self._in_script = False
        self._script_type = ""
        self._script_buffer: list[str] = []
        self._skip_depth = 0

    # -- Hilfen ------------------------------------------------------------

    def _push_text(self, data: str) -> None:
        if not data:
            return
        self.chunks.append(data)
        self._text_len += len(data)

    @property
    def text(self) -> str:
        # Unveraendert lassen: Die Positionen der Links beziehen sich auf genau
        # diese Zeichenkette. Ein spaeteres Zusammenfassen von Leerraum wuerde
        # die Indizes verschieben und Preise falsch zuordnen.
        return "".join(self.chunks)

    # -- HTMLParser --------------------------------------------------------

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key.lower(): (value or "") for key, value in attrs}
        if tag in {"script", "style"}:
            self._in_script = tag == "script"
            self._script_type = attributes.get("type", "").lower()
            self._script_buffer = []
            return
        if tag in {"br", "p", "div", "li", "article", "tr", "span"}:
            self._push_text(" ")
        if tag == "a":
            anchor = {
                "href": attributes.get("href", ""),
                # eBay und Kleinanzeigen hinterlegen den vollen Titel oft im
                # title-Attribut, waehrend der Linktext gekuerzt ist.
                "title_attr": attributes.get("title", ""),
                "start": self._text_len,
                "text": "",
            }
            self.anchors.append(anchor)
            self._stack.append(anchor)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"}:
            if self._in_script and self._script_type.startswith("application/ld+json"):
                self.json_ld.append("".join(self._script_buffer))
            self._in_script = False
            self._script_buffer = []
            return
        if tag == "a" and self._stack:
            self._stack.pop()
        if tag in {"p", "div", "li", "article", "tr"}:
            self._push_text(" ")

    def handle_data(self, data: str) -> None:
        if self._in_script:
            self._script_buffer.append(data)
            return
        self._push_text(data)
        for anchor in self._stack:
            anchor["text"] = (anchor["text"] + " " + data).strip()


def _json_ld_listings(document: str, page_url: str) -> list[dict]:
    """Angebote aus schema.org-Daten ziehen (ItemList, Product, Offer)."""
    try:
        payload = json.loads(document.strip())
    except ValueError:
        return []
    candidates: list[dict] = []
    queue: list[object] = [payload]
    while queue:
        node = queue.pop(0)
        if isinstance(node, list):
            queue.extend(node)
            continue
        if not isinstance(node, dict):
            continue
        node_type = str(node.get("@type", ""))
        if node_type in {"Product", "Offer", "ListItem", "ItemList"}:
            candidates.append(node)
        for key in ("itemListElement", "item", "itemOffered", "offers", "@graph", "mainEntity"):
            if key in node:
                value = node[key]
                queue.extend(value if isinstance(value, list) else [value])
    listings: list[dict] = []
    for node in candidates:
        name = node.get("name") or node.get("title")
        offers = node.get("offers")
        if isinstance(offers, list):
            offers = offers[0] if offers else None
        url = node.get("url")
        price = None
        if isinstance(offers, dict):
            url = url or offers.get("url")
            name = name or offers.get("name")
            if offers.get("price") is not None:
                price = to_float(str(offers.get("price")))
        if price is None and isinstance(node.get("price"), (int, float, str)):
            price = to_float(str(node["price"]))
        if not name or not url:
            continue
        listings.append(
            {
                "title": str(name).strip(),
                "url": _absolute(str(url), page_url),
                "price": price,
            }
        )
    return listings


def _absolute(url: str, page_url: str) -> str:
    if url.startswith("http://") or url.startswith("https://"):
        return url
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        base = re.match(r"https?://[^/]+", page_url or "")
        return (base.group(0) if base else "https://www.kleinanzeigen.de") + url
    return url


def _base_url_from_links(anchors: list[dict]) -> str:
    """Ersatzbasis, wenn die gespeicherte Seite keine canonical-URL nennt.

    Relative Links ("/itm/123") bekommen so die Domain der Seite, von der sie
    stammen - statt einer geratenen Plattform.
    """
    counts: dict[str, int] = {}
    for anchor in anchors:
        match = re.match(r"https?://([^/]+)", anchor.get("href", ""))
        if match:
            counts[match.group(1)] = counts.get(match.group(1), 0) + 1
    if not counts:
        return ""
    domain = max(counts.items(), key=lambda item: item[1])[0]
    return f"https://{domain}"


def _anchors_listings(
    parser: _PageParser, page_url: str, hints: tuple[str, ...], debug: list[str] | None
) -> list[dict]:
    text = parser.text
    listings: list[dict] = []
    for anchor in parser.anchors:
        href = anchor["href"]
        if not href or not any(hint in href for hint in hints):
            continue
        title = re.sub(r"\s+", " ", anchor["text"]).strip()
        fallback = re.sub(r"\s+", " ", anchor.get("title_attr") or "").strip()
        # Beide Plattformen setzen im title-Attribut den vollen Titel, waehrend
        # der Linktext gekuerzt ist - dann gilt das Attribut.
        if len(fallback.split()) >= 2 and len(fallback) > len(title):
            title = fallback
        # Kurze Linktexte sind Navigation ("Kategorie", "Mehr"), keine Anzeige:
        # ein brauchbarer Titel hat mehrere Woerter.
        if len(title) < 6 or len(title.split()) < 2:
            continue
        context = re.sub(
            r"\s+",
            " ",
            text[anchor["start"] : anchor["start"] + CONTEXT_WINDOW],
        )
        price_match = PRICE_RE.search(context)
        postal_match = POSTAL_RE.search(context)
        date_match = DATE_RE.search(context)
        entry: dict = {
            "title": title,
            "url": _absolute(href.split("?")[0], page_url),
        }
        if price_match:
            raw_amount = price_match.group("value1") or price_match.group("value2")
            entry["price"] = to_float(raw_amount)
        if postal_match:
            entry["postal_code"] = postal_match.group("postal")
            entry["location"] = postal_match.group("city").strip()
        else:
            city_match = CITY_ONLY_RE.search(context)
            if city_match:
                entry["location"] = city_match.group("city").strip()
        if date_match:
            day, month, year = (
                date_match.group("day"),
                date_match.group("month"),
                date_match.group("year"),
            )
            entry["published_at"] = f"{year}-{int(month):02d}-{int(day):02d}T00:00:00Z"
        listings.append(entry)
    if debug is not None:
        debug.append(f"Anzeigenlinks erkannt: {len(listings)} (Muster: {', '.join(hints)})")
    return listings


def parse_file(
    path: Path, hints: tuple[str, ...], debug: list[str], base_url: str = ""
) -> list[dict]:
    html = path.read_text(encoding="utf-8", errors="replace")
    parser = _PageParser()
    parser.feed(html)
    page_url = base_url
    if not page_url:
        match = re.search(
            r"<link[^>]+rel=[\"']canonical[\"'][^>]+href=[\"']([^\"']+)", html, re.I
        )
        if match:
            page_url = match.group(1)
        else:
            page_url = _base_url_from_links(parser.anchors)
    found: list[dict] = []
    for document in _json_ld_blocks(html):
        entries = _json_ld_listings(document, page_url)
        if entries:
            debug.append(f"{path.name}: {len(entries)} Angebote aus schema.org-Daten")
            found.extend(entries)
    anchor_entries = _anchors_listings(parser, page_url, hints, debug)
    for entry in anchor_entries:
        if not any(existing["url"] == entry["url"] for existing in found):
            found.append(entry)
    if not found:
        debug.append(
            f"{path.name}: nichts erkannt. Bitte die Datei pruefen (--debug) und die "
            "Link-Muster mit --link-pattern anpassen."
        )
    return found


def _json_ld_blocks(html: str) -> list[str]:
    blocks = []
    for match in re.finditer(
        r"<script[^>]*type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
        html,
        re.I | re.S,
    ):
        blocks.append(match.group(1))
    return blocks


def load_existing(target: Path) -> dict:
    if not target.is_file():
        return {"listings": []}
    try:
        document = json.loads(target.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise SystemExit(f"FEHLER: {target} ist kein gültiges JSON: {exc}")
    if not isinstance(document, dict) or not isinstance(document.get("listings"), list):
        raise SystemExit(f"FEHLER: {target} hat nicht das erwartete Format {{'listings': [...]}}")
    return document


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Selbst gespeicherte Suchseite in das Importformat der App umwandeln."
    )
    parser.add_argument("files", nargs="+", help="Gespeicherte HTML-Datei(en)")
    parser.add_argument("-o", "--output", required=True, help="Zieldatei (JSON)")
    parser.add_argument(
        "--merge", action="store_true", help="Bestehende Zieldatei ergänzen statt ersetzen"
    )
    parser.add_argument(
        "--link-pattern",
        action="append",
        default=None,
        help="Muster für Anzeigenlinks (mehrfach möglich, Standard: s-anzeige)",
    )
    parser.add_argument(
        "--base-url",
        default="",
        help="Basis-URL für relative Links (z. B. https://www.ebay.de), falls die Seite keine nennt",
    )
    parser.add_argument("--debug", action="store_true", help="Erkennung protokollieren")
    args = parser.parse_args(argv)

    hints = tuple(args.link_pattern) if args.link_pattern else DEFAULT_LINK_PATTERNS
    target = Path(args.output)
    debug: list[str] = []
    collected: list[dict] = []
    for raw in args.files:
        path = Path(raw)
        if not path.is_file():
            print(f"WARNUNG: {path} nicht gefunden - wird übersprungen.", file=sys.stderr)
            continue
        collected.extend(parse_file(path, hints, debug, args.base_url))

    # Dubletten über die URL entfernen, Reihenfolge beibehalten.
    unique: list[dict] = []
    seen: set[str] = set()
    for entry in collected:
        url = entry.get("url", "")
        if not url or url in seen:
            continue
        seen.add(url)
        unique.append(entry)

    if args.merge:
        document = load_existing(target)
        known = {entry.get("url") for entry in document["listings"]}
        added = [entry for entry in unique if entry.get("url") not in known]
        document["listings"].extend(added)
        if unique and not added:
            print("Keine neuen Angebote - alle URLs waren schon in der Zieldatei.")
    else:
        added = unique
        document = {
            "_hinweis": (
                "Aus selbst gespeicherten Suchseiten erzeugt (scripts/import_html.py). "
                "Die Angebote sind echt, aber Momentaufnahmen: Preise und Verfuegbarkeit "
                "koennen sich geaendert haben."
            ),
            "listings": added,
        }

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")

    without_price = [entry for entry in added if entry.get("price") is None]
    print(f"{len(added)} Angebote geschrieben nach {target}")
    if without_price:
        print(
            f"HINWEIS: {len(without_price)} Angebote ohne erkannten Preis - sie werden "
            "importiert, aber ohne Preisvorteil bewertet."
        )
    print(
        "So einbinden: LOCAL_IMPORT_PATH in .env auf diese Datei setzen und die App neu starten."
    )
    if args.debug:
        print("--- Erkennung ---")
        for line in debug:
            print(" ", line)
    # 1 nur, wenn gar nichts erkannt wurde - eine reine Dubletten-Runde ist ok.
    return 0 if unique else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
