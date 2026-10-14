"""Kleinanzeigen-Website-Connector (Browser-gestützt, ohne Zugangsdaten).

Warum es diesen Connector gibt
-----------------------------
Kleinanzeigen betreibt KEINE öffentliche API für Suche oder Angebotsdaten. Die
öffentliche Website lässt sich dagegen ohne Konto durchsuchen - genau das macht
ein Benutzer im Browser. Damit die App das ebenfalls kann, steuert dieser
Connector einen echten Browser über Playwright, öffnet die normale Suchseite
und liest die Ergebnisliste aus. Standardmäßig ist das Google Chrome
(``KLEINANZEIGEN_WEB_BROWSER=auto``); ohne Chrome läuft es mit Chromium oder
Firefox von Playwright.

Gleiches Vorgehen wie beim eBay-Website-Connector
-------------------------------------------------
* Ein realer Browser mit persistentem Profil liest die Seite so, wie ein
  Benutzer sie liest. Die Trefferliste ist serverseitig gerendert, deshalb
  läuft der Browser hier standardmäßig headless - es entsteht gar kein Fenster.
  Wird Headless abgeschaltet (``KLEINANZEIGEN_WEB_HEADLESS=false``), bleibt das
  Fenster trotzdem unsichtbar (außerhalb des Bildschirms);
  ``KLEINANZEIGEN_WEB_HIDDEN=false`` zeigt es dann wirklich an.
* Keine Umgehung technischer Schutzmaßnahmen: nur die öffentliche Suchseite,
  mit Pausen zwischen den Aufrufen, keine Anmeldung, kein Konto.
* Abschaltbar (``KLEINANZEIGEN_WEB_ENABLED=false``). Nicht erlaubt? Dann bleibt
  der Connector aus und meldet das ehrlich.

Ehrliche Grenzen (werden als Einschränkung gemeldet, nicht verschwiegen)
------------------------------------------------------------------------
* Automatisiertes Auslesen ist von den Nutzungsbedingungen nicht ausdrücklich
  erlaubt. Der Connector ist deshalb abschaltbar.
* Kleinanzeigen kennt keine Auktionen: jeder Treffer ist ein Festpreis, viele
  "Verhandlungsbasis" (VB). Ein Formatfilter ist damit gegenstandslos.
* Die Ergebnisliste nennt keinen Zustand. Er wird aus Titel, Beschreibung und
  Kartentext **abgeleitet** ("Gebraucht, gut", "Sehr gut / wie neu"); nennt der
  Text nichts, bleibt er leer statt geschätzt. Ausgeschlossen wird daraus nie:
  "Neu" und "Defekt" entscheidet weiterhin die Ingestion mit ihren harten
  Regeln über Titel und Beschreibung.
* Das Einstelldatum steht nur bei einem Teil der Angebote ("Heute, 20:40",
  "Gestern, 08:06", "08.10.2026", "08.10."). Es wird gelesen, wenn es da ist,
  sonst ehrlich leer gelassen.
* Versandkosten stehen nicht in der Ergebnisliste; die Gesamtkosten sind
  deshalb geschätzt. Ob ein Angebot versendet wird, sagt die Liste dagegen
  selbst ("Versand möglich" auf der Karte). Eine reine Versandsuche nutzt das
  und lässt Suchläufe ohne die Angebote laufen, die nur abgeholt werden können.
* Ein Umkreisfilter wird nicht gesetzt (die Plattform braucht dafür ihren
  eigenen Ortsbezug). Der Standort wird nur gelesen, nicht gefiltert.
* Ändert Kleinanzeigen den Seitenaufbau, liefert der Connector 0 Treffer und
  eine Einschränkung - es werden keine Daten geraten oder erfunden.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, time, timedelta, timezone
from typing import Any
from urllib.parse import quote, urlencode, urlparse

from ..domain.normalize import (
    CONDITION_GOOD,
    CONDITION_LABELS,
    CONDITION_VERY_GOOD,
    condition_rank_from_text,
)
from .base import (
    ConnectorCapabilities,
    ConnectorError,
    ConnectorUnavailableError,
    RawListing,
    SearchPage,
    SearchQuerySpec,
)
from .browser_web import BrowserWebConnector, german_float as _german_float

logger = logging.getLogger(__name__)

SEARCH_URL = "https://www.kleinanzeigen.de"
# Die Suchergebnisseite zeigt 25 Treffer je Seite ("1 - 25 von X Ergebnissen").
# Eine Seitengröße lässt sich nicht einstellen; geblättert wird über
# ?pageNum=N.
PAGE_SIZE = 25
_ALLOWED_HOSTS = ("www.kleinanzeigen.de", "kleinanzeigen.de", "www.ebay-kleinanzeigen.de")

_PRICE_RE = re.compile(r"(?:EUR|€)\s*([\d.,]+)", re.IGNORECASE)
_NUMBER_RE = re.compile(r"([\d.,]+)")
_TOTAL_RE = re.compile(r"von\s+([\d.]+)\s+Ergebnis", re.IGNORECASE)
_TOTAL_FALLBACK_RE = re.compile(r"([\d.]+)\s+Ergebnis", re.IGNORECASE)
_POSTAL_RE = re.compile(r"\b(\d{5})\b")
_AD_ID_RE = re.compile(r"/(\d{6,})-\d+-\d+")
_DATE_RE = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b")
_SHORT_DATE_RE = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(?!\d)")
_RELATIVE_RE = re.compile(r"\b(Heute|Gestern)\b(?:,?\s*(\d{1,2}):(\d{2}))?", re.IGNORECASE)

# Chip-Beschriftungen der Ergebnisliste. Sie sind die einzigen Angaben zu
# Versand/Abholung, die die Liste überhaupt nennt.
_BADGE_PICKUP = "Nur Abholung"
_BADGE_SHIPPING = "Versand möglich"

# Zustände, die aus dem Anzeigetext der Ergebnisliste abgeleitet werden dürfen.
# Bewusst **nur** "Gebraucht, gut" und "Sehr gut / wie neu":
#
# * Alles darüber ("Neu") und darunter ("Defekt") führt in der Ingestion zum
#   Ausschluss ("Neuware statt Gebrauchtware" bzw. "defekte Ware"). Ein
#   Fehltreffer im Freitext würde dort ein echtes Angebot löschen - beobachtet:
#   "...bereit für den Einsatz in einem neuen System" wurde zu "Neu",
#   "Keine Fehler oder Defekte!" zu "Defekt". Aus dem Text wird deshalb nie
#   ausgeschlossen, sondern nur die Reihenfolge verbessert.
# * Die abgeleitete Angabe muss die Bewertung tragen, nicht der Ausschluss.
_DERIVABLE_CONDITIONS = (CONDITION_GOOD, CONDITION_VERY_GOOD)

# Extrahiert die Angebotskarten aus der geladenen Suchergebnisseite. Der
# aktuelle Aufbau nutzt <article data-href=...> mit JSON-LD für Titel,
# Beschreibung und Bild; ältere Auslieferungen nutzen li.aditem. Beide Formen
# werden gelesen, damit ein Umbau nicht sofort 0 Treffer bedeutet.
_EXTRACT_JS = r"""
() => {
  const clean = (value) => (value || '').replace(/\s+/g, ' ').trim();
  const CHIPS = ['Direkt kaufen', 'Versand möglich', 'Nur Abholung', 'Reserviert',
                 'PRO', 'Gewerblich', 'Neu', 'Zu verschenken'];
  let cards = [...document.querySelectorAll('article[data-href]')];
  if (!cards.length) {
    cards = [...document.querySelectorAll('article.aditem, li.aditem')];
  }
  const items = [];
  for (const card of cards) {
    const link = card.querySelector('a[href*="/s-anzeige/"]');
    const href = card.getAttribute('data-href')
      || (link ? link.getAttribute('href') : '')
      || '';
    if (!href.includes('/s-anzeige/')) continue;

    let title = null;
    let description = null;
    let image = null;
    const ld = card.querySelector('script[type="application/ld+json"]');
    if (ld) {
      try {
        const parsed = JSON.parse(ld.textContent);
        title = clean(parsed.title) || null;
        description = clean(parsed.description) || null;
        image = parsed.contentUrl || null;
      } catch (err) { /* unlesbares JSON-LD ignorieren */ }
    }
    if (!title) {
      const heading = card.querySelector('h3 a, a.ellipsis, h3');
      title = heading ? clean(heading.innerText) : null;
    }
    if (!description) {
      const block = card.querySelector('p.text-bodyRegular, .aditem-main--middle--description');
      description = block ? clean(block.innerText) : null;
    }
    if (!image) {
      const img = card.querySelector('img[src]');
      image = img ? img.getAttribute('src') : null;
    }

    // Preis: kurzer Text mit Waehrung oder Kennung ("25 € VB", "VB",
    // "Zu verschenken"). Nur Elemente ohne eigene Kindelemente betrachten.
    let price = null;
    const legacyPrice = card.querySelector('.aditem-main--middle--price-shipping--price');
    if (legacyPrice) price = clean(legacyPrice.innerText) || null;
    if (!price) {
      for (const element of card.querySelectorAll('p, span')) {
        if (element.children.length) continue;
        const text = clean(element.innerText);
        if (!text || text.length > 30 || text === title) continue;
        if (/€|(^|\s)VB(\s|$)|Festpreis|verschenken/i.test(text)) { price = text; break; }
      }
    }

    // Standort steht neben dem Standort-Symbol, das Datum daneben in derselben Zeile.
    let location = null;
    let published = null;
    const marker = card.querySelector('svg[data-title="locationOutline"]');
    const markerBox = marker ? marker.parentElement : null;
    if (markerBox) location = clean(markerBox.innerText) || null;
    if (!location) {
      const legacyLocation = card.querySelector('.aditem-main--top--left');
      location = legacyLocation ? clean(legacyLocation.innerText) : null;
    }
    const topRow = markerBox && markerBox.parentElement ? markerBox.parentElement : null;
    if (topRow && topRow.children.length > 1) {
      const last = topRow.children[topRow.children.length - 1];
      published = clean(last.innerText) || null;
    }
    if (!published) {
      const legacyDate = card.querySelector('.aditem-main--top--right');
      published = legacyDate ? clean(legacyDate.innerText) : null;
    }

    // Chips ("Versand möglich", "Nur Abholung", ...) einsammeln. Fehlt der
    // Aufbau, bleibt die Liste leer - es wird nichts angenommen.
    const badges = [];
    const push = (text) => { if (text && !badges.includes(text)) badges.push(text); };
    for (const chip of card.querySelectorAll('span[data-dhl-promotion], p.flex > span')) {
      push(clean(chip.innerText));
    }
    for (const element of card.querySelectorAll('span, div')) {
      if (element.children.length) continue;
      const text = clean(element.innerText);
      if (CHIPS.includes(text)) push(text);
    }

    items.push({
      url: href.split('?')[0],
      listing_id: card.getAttribute('data-adid') || null,
      title: title,
      description: description,
      price: price,
      location: location,
      published: published,
      badges: badges,
      image: image,
      text: clean(card.innerText).slice(0, 600),
    });
  }
  const summary = document.querySelector('#srp-breadcrumb-summary');
  return {
    items: items,
    heading: summary ? clean(summary.innerText) : null,
  };
}
"""


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value).replace("\u00a0", " ")).strip()


def _clean_title(value: Any) -> str:
    """Titel bereinigen: mehrfache Leerzeichen und Zeilenumbrüche entfernen."""
    return _clean_text(value)


def _derive_condition(*texts: Any) -> str | None:
    """Zustand aus dem Anzeigetext der Ergebnisliste ableiten.

    Kleinanzeigen nennt in der Ergebnisliste keinen Zustand. Titel und
    Teaser-Snippet enthalten ihn aber oft ("wie neu", "kaum gebraucht"). Ohne
    diese Ableitung wird jedes Angebot der Quelle mit "Zustand unbekannt"
    bewertet, obwohl der Verkäufer den Zustand genannt hat - die Quelle mit der
    besten Trefferquote wurde dadurch systematisch abgewertet.

    Rückgabe ist die Bezeichnung des Zustands (z.B. "Sehr gut / wie neu") oder
    ``None``. Nur die Zustände aus ``_DERIVABLE_CONDITIONS`` werden übernommen;
    "Neu" und "Defekt" bleiben der Ingestion vorbehalten, die dafür Titel und
    Beschreibung mit den harten Regeln prüft.
    """
    text = _clean_text(" ".join(part for part in texts if part))
    rank = condition_rank_from_text(text)
    if rank in _DERIVABLE_CONDITIONS:
        return CONDITION_LABELS.get(rank)
    return None


def _absolute_url(raw: Any) -> str:
    """Mach relative Angebotslinks absolut; fremde Ziele werden verworfen.

    Rückgabe: absolute Angebots-URL ohne Query-Parameter, oder "" wenn der Link
    nicht zur Plattform gehört bzw. nicht lesbar ist.
    """
    text = _clean_text(raw)
    if not text:
        return ""
    if text.startswith("/"):
        path = text.split("?")[0].split("#")[0]
        return f"{SEARCH_URL}{path}" if "/s-anzeige/" in path else ""
    parsed = urlparse(text)
    if not parsed.scheme or not parsed.netloc:
        return ""
    if parsed.netloc.lower() not in _ALLOWED_HOSTS:
        return ""
    path = parsed.path or ""
    if "/s-anzeige/" not in path:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def _parse_price(raw: Any) -> float | None:
    """Preis aus der Ergebnisliste lesen. "VB" allein ist kein Preis."""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    text = _clean_text(raw)
    if not text:
        return None
    if "verschenken" in text.lower():
        return 0.0
    match = _PRICE_RE.search(text) or _NUMBER_RE.search(text)
    if match is None:
        return None
    return _german_float(match.group(1))


def _parse_total(heading: Any) -> int | None:
    """Trefferzahl aus '1 - 25 von 1.228 Ergebnissen' lesen."""
    text = _clean_text(heading)
    if not text:
        return None
    match = _TOTAL_RE.search(text) or _TOTAL_FALLBACK_RE.search(text)
    if match is None:
        return None
    value = _german_float(match.group(1))
    return int(value) if value is not None else None


def _parse_location(raw: Any) -> tuple[str | None, str | None]:
    """Rückgabe: (Postleitzahl, Ort). Nur was wirklich dasteht."""
    text = _clean_text(raw)
    if not text:
        return None, None
    match = _POSTAL_RE.search(text)
    if match is None:
        return None, text
    city = _clean_text(text.replace(match.group(1), " ").strip(" ,-"))
    return match.group(1), (city or None)


def _parse_published(raw: Any, *, now: datetime | None = None) -> datetime | None:
    """Einstelldatum lesen: 'Heute, 20:11', 'Gestern, 08:06', '08.10.2026', '08.10.'.

    Die Liste nennt die Uhrzeit in lokaler Zeit; gespeichert wird in UTC.
    Fehlt das Datum oder ist es unlesbar, kommt ``None`` zurück - es wird
    nichts geschätzt.
    """
    text = _clean_text(raw)
    if not text:
        return None
    reference = (now or datetime.now(timezone.utc)).astimezone()

    absolute = _DATE_RE.search(text)
    if absolute is not None:
        day, month, year = (int(part) for part in absolute.groups())
    else:
        short = _SHORT_DATE_RE.search(text)
        if short is not None:
            day, month = (int(part) for part in short.groups())
            year = reference.year
        else:
            relative = _RELATIVE_RE.search(text)
            if relative is None:
                return None
            day_offset = 0 if relative.group(1).lower() == "heute" else 1
            target = (reference - timedelta(days=day_offset)).date()
            hour = int(relative.group(2)) if relative.group(2) else 0
            minute = int(relative.group(3)) if relative.group(3) else 0
            try:
                return datetime.combine(target, time(hour, minute)).astimezone(timezone.utc)
            except ValueError:
                return None

    try:
        naive = datetime(year, month, day)
    except ValueError:
        return None
    return naive.astimezone(timezone.utc)


def _clean_badges(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    badges: list[str] = []
    for entry in value:
        text = _clean_text(entry)
        if text and text not in badges:
            badges.append(text)
    return badges[:8]


def _id_from_url(url: str) -> str | None:
    match = _AD_ID_RE.search(url or "")
    return match.group(1) if match else None


class KleinanzeigenWebConnector(BrowserWebConnector):
    """Kleinanzeigen-Suche über die öffentliche Website mit lokalem Chrome."""

    platform = "kleinanzeigen_web"
    settings_prefix = "kleinanzeigen_web"
    platform_label = "Kleinanzeigen"
    probe_term = "grafikkarte"
    warmup_url = "https://www.kleinanzeigen.de/"
    ready_selector = "article[data-href], article.aditem, li.aditem"
    extract_js = _EXTRACT_JS
    default_profile_dir = "data/kleinanzeigen_profile"
    # Die Trefferliste kommt fertig gerendert vom Server - kein Fenster nötig.
    default_headless = True
    blocked_title_markers = ("Zugriff verweigert", "Error Page")

    # -- Capabilities ------------------------------------------------------

    @property
    def capabilities(self) -> ConnectorCapabilities:
        enabled = self.enabled
        playwright_ok = self._playwright_available()
        configured = enabled and playwright_ok
        reason = self._unavailable_reason(
            "Kleinanzeigen-Website-Suche ist deaktiviert "
            "(KLEINANZEIGEN_WEB_ENABLED=false)."
        )
        return ConnectorCapabilities(
            platform=self.platform,
            label="Kleinanzeigen (Website)",
            enabled=enabled,
            configured=configured,
            available=configured,
            supports_search=configured,
            supports_pagination=configured,
            supports_details=False,
            # Kleinanzeigen kennt keine Auktionen - jeder Treffer ist Festpreis.
            supports_server_side_fixed_price_filter=False,
            supports_price_filter=configured,
            supports_condition_filter=False,
            supports_location_filter=False,
            supports_days_back_filter=False,
            supports_shipping_info=configured,
            supports_pickup_info=configured,
            provides_published_date=True,
            provides_seller_info=False,
            provides_images=True,
            page_size_max=PAGE_SIZE,
            max_results_per_query=int(self._setting("max_results_per_query", 500)),
            docs_url=None,
            requires_credentials=False,
            setup_hint=(
                self.browser_setup_hint()
                + " Browser wählbar über KLEINANZEIGEN_WEB_BROWSER "
                "(auto, chrome, chromium, firefox). "
                "Abschalten mit KLEINANZEIGEN_WEB_ENABLED=false."
            ),
            limitations=[
                "Liest die öffentliche Kleinanzeigen-Website über einen Browser "
                "aus (keine API, keine Zugangsdaten).",
                "Kleinanzeigen kennt keine Auktionen: alle Angebote sind Festpreise, "
                "häufig Verhandlungsbasis (VB).",
                "Die Ergebnisliste nennt keinen Zustand; er wird aus dem "
                "Anzeigetext abgeleitet und bleibt ohne Hinweis leer statt "
                "geschätzt. Einen Zustandsfilter bietet die Plattform nicht.",
                "Ein Veröffentlichungsdatum steht nur bei einem Teil der Angebote.",
                "Versandkosten stehen nicht in der Ergebnisliste; die Gesamtkosten "
                "sind deshalb geschätzt.",
                "Ein Umkreisfilter wird nicht gesetzt - der Suchlauf findet auch "
                "Angebote außerhalb der eigenen Reichweite.",
                "Eine reine Versandsuche wird von der Plattform selbst gefiltert "
                "(\"Versand möglich\"); für die Abholsuche gibt es dort keinen "
                "gleichwertigen Filter.",
                "Ändert Kleinanzeigen den Seitenaufbau, liefert der Connector 0 "
                "Treffer statt geratener Daten.",
            ],
            reason_unavailable=reason,
        )

    # -- URL-Bau -----------------------------------------------------------

    @staticmethod
    def _slug(term: str) -> str:
        """Suchbegriff als Pfadsegment ('rtx 3060' -> 'rtx%203060')."""
        return quote(_clean_text(term).replace("/", " "), safe="")

    def _build_url(self, query: SearchQuerySpec, *, page: int = 1) -> str:
        slug = self._slug(query.term) or self._slug(self.probe_term)
        params: dict[str, str] = {}
        if page > 1:
            params["pageNum"] = str(page)
        if query.max_price is not None:
            params["maxPrice"] = f"{query.max_price:g}"
        if query.min_price is not None:
            params["minPrice"] = f"{query.min_price:g}"
        # Die Liste hat keinen echten Datumsfilter, aber eine Sortierung:
        # neueste Angebote zuerst.
        params["sortingField"] = str(self._setting("sorting_field", "SORTING_DATE"))
        # Reine Versandsuche: Die Plattform ordnet jedes Angebot selbst als
        # "Versand möglich" oder "Nur Abholung" ein und filtert danach
        # serverseitig (/s-versand:ja/...). Damit kommen Abholangebote gar nicht
        # erst in den Lauf - statt sie danach wegzuwerfen.
        prefix = "s-versand:ja/" if query.shipping_only else "s-"
        return f"{SEARCH_URL}/{prefix}{slug}/k0?{urlencode(params)}"

    # -- Abruf -------------------------------------------------------------

    async def search_page(self, query: SearchQuerySpec) -> SearchPage:
        caps = self.capabilities
        if not caps.available:
            raise ConnectorUnavailableError(
                caps.reason_unavailable or "Kleinanzeigen-Website ist nicht verfügbar.",
                platform=self.platform,
            )

        page_number = max(1, query.offset // PAGE_SIZE + 1)
        url = self._build_url(query, page=page_number)

        async with self._semaphore:
            await self._pace()
            data = await self._fetch(url)
            self._last_fetch_at = asyncio.get_running_loop().time()

        limitations: list[str] = []
        if data.get("blocked"):
            limitations.append(
                "Kleinanzeigen hat den automatischen Abruf blockiert (Bot-Schutz). "
                "Bitte den Suchlauf später wiederholen oder den Connector abschalten "
                "(KLEINANZEIGEN_WEB_ENABLED=false)."
            )
            return SearchPage(
                listings=[],
                offset=query.offset,
                limit=PAGE_SIZE,
                total_reported=None,
                has_next=False,
                truncated=True,
                limitations=limitations,
                meta={"blocked": True, "url": url},
            )

        raw_items = data.get("items") or []
        listings: list[RawListing] = []
        parse_errors = 0
        seen: set[str] = set()
        for item in raw_items:
            try:
                listing = self._to_raw_listing(item)
            except Exception as exc:  # Einzelfehler darf nichts abbrechen
                parse_errors += 1
                logger.debug("Kleinanzeigen-Treffer nicht lesbar: %s", exc)
                continue
            if listing is None:
                continue
            key = listing.platform_listing_id or listing.url
            if key in seen:
                continue
            seen.add(key)
            listings.append(listing)

        total = _parse_total(data.get("heading"))
        if total is not None and total < len(listings):
            total = None
        if not listings and not raw_items:
            limitations.append(
                "Die Kleinanzeigen-Ergebnisseite enthielt keine lesbaren Angebote. "
                "Möglicherweise hat sich der Seitenaufbau geändert oder der "
                "Suchbegriff lieferte nichts."
            )
        if parse_errors:
            limitations.append(
                f"{parse_errors} Kleinanzeigen-Treffer konnten nicht gelesen werden."
            )

        if total is not None:
            has_next = query.offset + PAGE_SIZE < total
        else:
            has_next = len(raw_items) >= PAGE_SIZE
        next_offset = query.offset + PAGE_SIZE if has_next else None

        return SearchPage(
            listings=listings,
            offset=query.offset,
            limit=PAGE_SIZE,
            total_reported=total,
            has_next=has_next,
            next_offset=next_offset,
            truncated=bool(limitations),
            limitations=limitations,
            meta={"url": url, "page": page_number, "source": "kleinanzeigen_web"},
        )

    # -- Parsing -----------------------------------------------------------

    def _to_raw_listing(self, item: dict[str, Any]) -> RawListing | None:
        url = _absolute_url(item.get("url"))
        title = _clean_title(item.get("title"))
        if not url or not title:
            return None

        price_text = _clean_text(item.get("price"))
        price = _parse_price(price_text)
        postal_code, city = _parse_location(item.get("location"))
        published_at = _parse_published(item.get("published"))
        badges = _clean_badges(item.get("badges"))
        listing_id = item.get("listing_id") or _id_from_url(url)
        description = _clean_text(item.get("description")) or None
        # Der Zustand steht nicht in der Liste - er wird aus Titel, Beschreibung
        # und Kartentext abgeleitet, wo er genannt ist (siehe _derive_condition).
        condition_text = _derive_condition(title, description, item.get("text"))

        return RawListing(
            platform=self.platform,
            platform_listing_id=str(listing_id) if listing_id else None,
            url=url,
            title=title,
            description=description,
            price=price,
            currency="EUR",
            # "Nur Abholung" ist die einzige Angabe, die die Liste zu Abholung
            # macht. Fehlt sie, wird nichts angenommen.
            pickup_available=_BADGE_PICKUP in badges,
            # Kleinanzeigen kennt keine Auktionen: jeder Treffer ist ein
            # Festpreisangebot (auch "VB" ist kein Auktionsformat).
            buying_options=["FIXED_PRICE"],
            platform_format="FIXED_PRICE",
            condition_text=condition_text,
            published_at=published_at,
            location_text=city,
            postal_code=postal_code,
            country="DE",
            image_url=item.get("image") or None,
            extra={
                "source": "kleinanzeigen_web",
                "price_text": price_text or None,
                "badges": badges,
                # Hinweise aus der Liste - mehr steht dort nicht.
                "shipping_possible": _BADGE_SHIPPING in badges,
                "card_text": _clean_text(item.get("text"))[:400],
                # Herkunft des Zustands: Die Liste nennt keinen, die Ableitung
                # aus dem Anzeigetext muss nachvollziehbar bleiben.
                "condition_from_text": condition_text is not None,
            },
        )

    # -- Diagnose ----------------------------------------------------------

    async def health_check(self) -> tuple[bool, str | None]:
        caps = self.capabilities
        if not caps.available:
            return False, caps.reason_unavailable
        return True, f"{self.browser_hint()} wird für die Kleinanzeigen-Suche gesteuert."

    async def test_connection(self) -> tuple[bool, str | None, dict[str, Any]]:
        caps = self.capabilities
        if not caps.available:
            return False, caps.reason_unavailable, {}
        query = SearchQuerySpec(
            term=self.probe_term,
            platform=self.platform,
            page_size=PAGE_SIZE,
            max_pages=1,
        )
        try:
            page = await self.search_page(query)
        except ConnectorError as exc:
            return False, str(exc), {}
        if page.meta.get("blocked"):
            return (
                False,
                "Kleinanzeigen hat den automatischen Abruf blockiert (Bot-Schutz). "
                "Bitte später erneut versuchen.",
                {"blocked": True, "url": page.meta.get("url")},
            )
        if not page.listings:
            return (
                False,
                "Die Kleinanzeigen-Suchseite lieferte keine lesbaren Angebote. "
                "Möglicherweise hat sich der Seitenaufbau geändert.",
                {"url": page.meta.get("url")},
            )
        sample = page.listings[0]
        return (
            True,
            None,
            {
                "ergebnis": f"{page.count} Angebote für '{self.probe_term}' gelesen",
                "beispiel_titel": sample.title,
                "beispiel_preis": sample.price,
                "beispiel_link": sample.url,
            },
        )
