"""eBay-Website-Connector (Browser-gestützt, ohne Zugangsdaten).

Warum es diesen Connector gibt
-----------------------------
Die offizielle eBay-Browse-API (Connector ``ebay``) verlangt Entwickler-
Zugangsdaten (EBAY_CLIENT_ID / EBAY_CLIENT_SECRET). Die öffentliche eBay-
Website lässt sich dagegen ohne Konto durchsuchen - genau das macht ein
Benutzer im Browser. Damit die App das ebenfalls kann, steuert dieser
Connector den lokal installierten Google Chrome über Playwright, öffnet die
normale Suchseite und liest die Ergebnisliste aus.

Warum ein echter (aber unsichtbarer) Browser nötig ist
--------------------------------------
eBay blockt einfache HTTP-Abrufe und auch Headless-Browser mit ``HTTP 403``
(Bot-Schutz). Nur ein realer Browser erhält die Ergebnisseite - ein sichtbares
Fenster ist dafür aber nicht nötig: Der Browser läuft standardmäßig außerhalb
des Bildschirms und minimiert (``EBAY_WEB_HIDDEN=false`` zeigt ihn wieder).
Deshalb läuft der Browser nicht headless.

Ehrliche Grenzen (werden als Einschränkung gemeldet, nicht verschwiegen)
-----------------------------------------------------------------------
* Automatisiertes Auslesen ist von eBays Nutzungsbedingungen nicht ausdrücklich
  erlaubt. Der Connector ist deshalb abschaltbar (``EBAY_WEB_ENABLED``).
* Kein Veröffentlichungsdatum in der Ergebnisliste -> der Zeitraum wird nur
  über die Sortierung "neu eingestellt" angenähert.
* Versandkosten und Standort stehen nicht zuverlässig in der Ergebnisliste.
* Ändert eBay den Seitenaufbau, liefert der Connector 0 Treffer und eine
  Einschränkung - es werden keine Daten geraten oder erfunden.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any
from urllib.parse import urlencode

from .base import (
    ConnectorCapabilities,
    ConnectorError,
    ConnectorUnavailableError,
    RawListing,
    SearchPage,
    SearchQuerySpec,
)
from .browser_web import BrowserWebConnector
from .browser_web import german_float as _german_float

logger = logging.getLogger(__name__)

SEARCH_URL = "https://www.ebay.de/sch/i.html"
# eBay liefert pro Seite standardmäßig 60 Einträge; 120/240 sind ebenfalls
# möglich und liefern dieselbe Liste in weniger Anfragen (also: mehr Treffer je
# Anfrage, weniger Last). Live kann der Server die Seitengröße trotzdem
# kleiner halten - siehe ``_observed_sizes``: Die Schrittweite richtet sich
# nach der tatsächlich gelieferten Seite, damit keine Lücke entsteht.
ALLOWED_PAGE_SIZES = (60, 120, 240)
DEFAULT_PAGE_SIZE = 60
# Standardmäßig angefragte Seitengröße (überschreibbar mit EBAY_WEB_PAGE_SIZE).
DEFAULT_CONFIGURED_PAGE_SIZE = 240
# Obergrenze für die gemerkten Seitengrößen (nur Diagnose/Robustheit).
MAX_REMEMBERED_SIZES = 500

# Zustandscodes der eBay-Website (LH_ItemCondition).
_CONDITION_CODES: dict[str, str] = {
    "neu": "1000",
    "neu: sonstige": "1500",
    "neu mit mängeln": "1750",
    "vom hersteller generalüberholt": "2000",
    "vom verkäufer generalüberholt": "2500",
    "gebraucht": "3000",
    "sehr gut": "4000",
    "gut": "5000",
    "akzeptabel": "6000",
    "als ersatzteil / defekt": "7000",
}

# Zustandscodes je Rang (siehe backend/domain/normalize.CONDITION_*). Der
# Suchlauf übergibt die Ränge als Zahlen; die Namen sind für die Oberfläche.
_RANK_CONDITION_CODES: dict[str, str] = {
    "1": "6000",  # Akzeptabel
    "2": "5000",  # Gebraucht, gut
    "3": "4000",  # Sehr gut / wie neu
}
# "Gebraucht" ohne Unterzustand: Items, die eBay nur allgemein als gebraucht
# führt, tragen diesen Code. Ohne ihn fehlten sie in der Trefferliste.
_USED_PARENT_CODE = "3000"

_ITEM_ID_RE = re.compile(r"/itm/(\d+)")
_PRICE_RE = re.compile(r"(?:EUR|€|USD|\$)\s*([\d.,]+)", re.IGNORECASE)
_TOTAL_RE = re.compile(r"([\d.,]+)\s*Ergebnis", re.IGNORECASE)
_FEEDBACK_RE = re.compile(r"(\d{1,3})\s*%\s*(?:positiv|positive)", re.IGNORECASE)
_SELLER_RE = re.compile(
    r"([\w.\-]{2,})\s+\d{1,3}\s*%\s*(?:positiv|positive)", re.IGNORECASE
)
_WINDOW_SUFFIX_RE = re.compile(
    r"\s*Wird in neuem Fenster oder Tab ge(?:ö|oe)ffnet\s*$", re.IGNORECASE
)
_NEW_BADGE_RE = re.compile(r"^(?:NEUES ANGEBOT|Neues Angebot)\s*", re.IGNORECASE)

# Extrahiert die Angebotskarten aus der geladenen Suchergebnisseite.
_EXTRACT_JS = r"""
() => {
  // Nur die echte Trefferliste auswerten: eBay hängt unten Karussells mit
  // "ähnlichen Angeboten" an, die sonst die Ergebnisse verfälschen würden.
  let cards = [...document.querySelectorAll(
    '#srp-river-results ul.srp-results li.s-card, #srp-river-results li.s-card')];
  if (!cards.length) {
    cards = [...document.querySelectorAll('li.s-card, div.s-card')];
  }
  const items = [];
  for (const c of cards) {
    const link = c.querySelector('a.s-card__link[href*="/itm/"]');
    if (!link) continue;
    const href = link.getAttribute('href') || '';
    if (href.includes('/itm/123456')) continue;  // eBay-Platzhalterkarte
    const grab = (sel) => {
      const e = c.querySelector(sel);
      return e ? e.innerText.replace(/\s+/g, ' ').trim() : null;
    };
    const img = c.querySelector('img.s-card__image');
    items.push({
      url: href.split('?')[0],
      listing_id: c.getAttribute('data-listingid') || null,
      title: grab('.s-card__title'),
      price: grab('.s-card__price'),
      subtitle: grab('.s-card__subtitle'),
      footer: grab('.s-card__footer'),
      text: (c.innerText || '').replace(/\s+/g, ' ').trim(),
      image: img ? (img.getAttribute('src') || null) : null,
    });
  }
  const heading = document.querySelector('.srp-controls__count-heading');
  return {
    items: items,
    heading: heading ? heading.innerText.replace(/\s+/g, ' ').trim() : null,
  };
}
"""


def _effective_page_size(requested: int | None) -> int:
    """Rundet die gewünschte Seitengröße auf einen von eBay erlaubten Wert."""
    value = requested or DEFAULT_PAGE_SIZE
    for size in ALLOWED_PAGE_SIZES:
        if value <= size:
            return size
    return ALLOWED_PAGE_SIZES[-1]


def _parse_price(raw: Any) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw)
    if not text.strip():
        return None
    if "gratis" in text.lower():
        return 0.0
    match = _PRICE_RE.search(text)
    if match is None:
        match = re.search(r"([\d.,]+)", text)
        if match is None:
            return None
    return _german_float(match.group(1))


def _parse_total(heading: Any) -> int | None:
    if not heading:
        return None
    match = _TOTAL_RE.search(str(heading))
    if match is None:
        return None
    value = _german_float(match.group(1).rstrip("."))
    return int(value) if value is not None else None


def _clean_title(raw: Any) -> str:
    if raw is None:
        return ""
    title = str(raw).replace("\u00a0", " ").strip()
    title = _WINDOW_SUFFIX_RE.sub("", title)
    title = _NEW_BADGE_RE.sub("", title)
    return re.sub(r"\s+", " ", title).strip()


def _item_id_from_url(url: str) -> str | None:
    match = _ITEM_ID_RE.search(url or "")
    return match.group(1) if match else None


class EbayWebConnector(BrowserWebConnector):
    """eBay-Suche über die öffentliche Website mit lokalem Chrome."""

    platform = "ebay_web"
    settings_prefix = "ebay_web"
    platform_label = "eBay"
    probe_term = "grafikkarte"
    warmup_url = "https://www.ebay.de/"
    ready_selector = "#srp-river-results li.s-card, li.s-card, div.s-card"
    extract_js = _EXTRACT_JS
    default_profile_dir = "data/ebay_profile"
    blocked_title_markers = ("Error Page",)
    # Zweite Suchansicht: "neueste zuerst". Die Standardsicht (Best Match) und
    # diese Ansicht zeigen unterschiedliche Ausschnitte derselben Suche; bei
    # breiten Suchbegriffen holt der Suchlauf deshalb beide.
    sort_variants = ("newest",)

    def __init__(self, settings) -> None:
        super().__init__(settings)
        # Tatsächlich gelieferte Seitengröße je Suchbegriff (siehe search_page).
        self._observed_sizes: dict[tuple, int] = {}

    # -- Seitengröße -------------------------------------------------------

    def _size_key(self, query: SearchQuerySpec) -> tuple:
        return (query.term, query.min_price, query.max_price)

    def requested_page_size(self, query: SearchQuerySpec) -> int:
        """Angefragte Seitengröße: konfigurierter Wert, aber nie kleiner als gewünscht."""
        configured = int(
            self._setting("page_size", DEFAULT_CONFIGURED_PAGE_SIZE)
            or DEFAULT_CONFIGURED_PAGE_SIZE
        )
        configured = _effective_page_size(configured)
        wanted = max(int(query.page_size or 0), configured)
        return _effective_page_size(wanted)

    @property
    def capabilities(self) -> ConnectorCapabilities:
        enabled = self.enabled
        playwright_ok = self._playwright_available()
        configured = enabled and playwright_ok
        reason: str | None = None
        if not enabled:
            reason = "eBay-Website-Suche ist deaktiviert (EBAY_WEB_ENABLED=false)."
        elif not playwright_ok:
            reason = (
                "Playwright ist nicht installiert. Bitte 'pip install playwright' "
                "ausführen; Google Chrome muss installiert sein."
            )
        return ConnectorCapabilities(
            platform=self.platform,
            label="eBay (Website)",
            enabled=enabled,
            configured=configured,
            available=configured,
            supports_search=configured,
            supports_pagination=configured,
            supports_details=False,
            supports_server_side_fixed_price_filter=configured,
            supports_price_filter=configured,
            supports_condition_filter=configured,
            # Geprüft und verworfen: Die Parameter _stpos/_sadis verändern die
            # Trefferliste der Website nicht (gleiche Artikel bei 1 km Umkreis
            # wie ohne Filter). Die Website liefert außerdem keinen
            # Verkäuferstandort je Angebot - der Standortfilter wirkt hier
            # deshalb weder auf der Quelle noch lokal.
            supports_location_filter=False,
            supports_days_back_filter=False,
            supports_shipping_info=False,
            supports_pickup_info=False,
            provides_published_date=False,
            provides_seller_info=True,
            provides_images=True,
            page_size_max=ALLOWED_PAGE_SIZES[-1],
            max_results_per_query=int(
                getattr(self.settings, "ebay_web_max_results_per_query", 480)
            ),
            docs_url=None,
            requires_credentials=False,
            setup_hint=(
                "Google Chrome installieren und 'pip install playwright' ausführen. "
                "Abschalten mit EBAY_WEB_ENABLED=false."
            ),
            limitations=[
                "Liest die öffentliche eBay-Website über einen echten Browser "
                "aus (keine API, keine Zugangsdaten); das Fenster bleibt "
                "unsichtbar (EBAY_WEB_HIDDEN=false zeigt es).",
                "Der Preisrahmen (Mindestpreis und Kauf-Limit) geht als "
                "_udlo/_udhi an eBay. eBay füllt einen engen Rahmen mit "
                "ähnlichen Artikeln auf; die Titelprüfung verwirft sie.",
                "Kein Veröffentlichungsdatum verfügbar - der Zeitraum wird nur "
                "über die Sortierung 'neu eingestellt' angenähert.",
                "Versandkosten und Standort stehen nicht zuverlässig in der "
                "Ergebnisliste; die Gesamtkosten sind deshalb geschätzt.",
                "Ändert eBay den Seitenaufbau, liefert der Connector 0 Treffer "
                "statt geratener Daten; dann bitte die Zugangsdaten-API nutzen.",
            ],
            reason_unavailable=reason,
        )

    # -- URL-Bau -----------------------------------------------------------

    def _build_url(
        self, query: SearchQuerySpec, *, page_size: int, page: int
    ) -> str:
        params: dict[str, str] = {
            "_nkw": query.term,
            "_ipg": str(page_size),
        }
        if page > 1:
            params["_pgn"] = str(page)
        if query.fixed_price_only:
            params["LH_BIN"] = "1"
        # Preisrahmen auf der Quelle: eBay liefert dann nur noch Angebote
        # zwischen Mindestpreis (_udlo) und Kauf-Limit (_udhi).
        #
        # Das ist bewusst aktiv (so gewünscht), obwohl die Grenzen des Rahmens
        # live vermessen wurden: eBay füllt einen engen Rahmen mit ähnlichen
        # Artikeln auf, wenn es zu wenige genaue Treffer gibt. In einem echten
        # Suchlauf war mit Preisrahmen bei 879 von 902 geprüften Titeln das
        # genannte Modell ein anderes (97 %), ohne Rahmen bei 49 von 935 (5 %).
        # Der Rahmen spart also das Lesen überteuerter Treffer, kostet aber
        # Anfragen für die Auffüll-Artikel - die Titelprüfung verwirft sie.
        #
        # Die letzte Instanz bleibt trotzdem ``ingest``: Es misst jedes Angebot
        # erneut am Kauf-Limit seiner Komponente und am Mindestpreis (ein
        # Viertel davon). Ein auf der Quelle ignorierter Rahmen fällt dort auf.
        if query.max_price is not None:
            params["_udhi"] = f"{query.max_price:g}"
        if query.min_price is not None:
            params["_udlo"] = f"{query.min_price:g}"
        condition_codes = self._condition_codes(query)
        if condition_codes:
            params["LH_ItemCondition"] = "|".join(condition_codes)
        if query.sort == "newest":
            # Zweite Ansicht: Sortierung "neu eingestellt". Das ist kein echter
            # Datumsfilter (eBay liefert in der Ergebnisliste kein Datum), aber
            # der einzige Weg, an frische Angebote zu kommen, die die
            # Relevanzsortierung weit unten führt.
            params["_sop"] = "10"
        return f"{SEARCH_URL}?{urlencode(params)}"

    @staticmethod
    def _condition_codes(query: SearchQuerySpec) -> list[str]:
        """Zustandscodes der Anfrage - als Name oder als Rangzahl.

        Der Suchlauf übergibt die *gebrauchten* Zustände, damit Neuware gar
        nicht erst geliefert wird. "Gebraucht" (3000) wird ergänzt: Items ohne
        Unterzustand tragen nur diesen Code.
        """
        codes: list[str] = []
        used = False
        for text in query.condition_texts or []:
            key = str(text).strip().lower()
            code = _CONDITION_CODES.get(key) or _RANK_CONDITION_CODES.get(key)
            if key in _RANK_CONDITION_CODES:
                used = True
            if code and code not in codes:
                codes.append(code)
        if used and _USED_PARENT_CODE not in codes:
            codes.append(_USED_PARENT_CODE)
        return codes

    # -- Abruf -------------------------------------------------------------

    async def search_page(self, query: SearchQuerySpec) -> SearchPage:
        caps = self.capabilities
        if not caps.available:
            raise ConnectorUnavailableError(
                caps.reason_unavailable or "eBay-Website ist nicht verfügbar.",
                platform=self.platform,
            )

        requested = self.requested_page_size(query)
        # Liefert eBay kleiner als angefragt, wird mit der kleineren (erlaubten)
        # Seitengröße weitergeblättert - sonst entstünde eine Lücke zwischen den
        # Seiten.
        page_size = self._observed_sizes.get(self._size_key(query), requested)
        page = max(1, query.offset // page_size + 1)
        url = self._build_url(query, page_size=page_size, page=page)

        async with self._semaphore:
            await self._pace()
            data = await self._fetch(url)
            self._last_fetch_at = asyncio.get_running_loop().time()

        limitations: list[str] = []
        if data.get("blocked"):
            limitations.append(
                "eBay hat den automatischen Abruf blockiert (Bot-Schutz, HTTP 403). "
                "Bitte den Suchlauf wiederholen oder die Zugangsdaten-API "
                "(EBAY_CLIENT_ID/EBAY_CLIENT_SECRET) nutzen."
            )
            return SearchPage(
                listings=[],
                offset=query.offset,
                limit=page_size,
                total_reported=None,
                has_next=False,
                truncated=True,
                limitations=limitations,
                meta={"blocked": True, "url": url},
            )

        raw_items = data.get("items") or []
        observed = len(raw_items)
        if 0 < observed < requested:
            # eBay hält sich nicht an die angefragte Seitengröße. Die kleinere,
            # erlaubte Größe merken, damit die nächste Seite direkt daran
            # anschließt (statt Treffer zu überspringen).
            if len(self._observed_sizes) >= MAX_REMEMBERED_SIZES:
                self._observed_sizes.clear()
            page_size = _effective_page_size(observed)
            self._observed_sizes[self._size_key(query)] = page_size
        listings: list[RawListing] = []
        parse_errors = 0
        seen: set[str] = set()
        for item in raw_items:
            try:
                listing = self._to_raw_listing(
                    item, fixed_price_only=query.fixed_price_only
                )
            except Exception as exc:  # Einzelfehler darf nichts abbrechen
                parse_errors += 1
                logger.debug("eBay-Website-Treffer nicht lesbar: %s", exc)
                continue
            if listing is None:
                continue
            key = listing.platform_listing_id or listing.url
            if key in seen:
                continue
            seen.add(key)
            listings.append(listing)

        # Die Anzeige "N Ergebnisse" der Seite ist unzuverlässig (sie zählt oft
        # nur einen Teil, während die Liste verwandte Angebote einmischt).
        # Deshalb steuert die tatsächliche Listengröße die Paginierung.
        total = _parse_total(data.get("heading"))
        if total is not None and total < len(listings):
            total = None
        if not listings and not raw_items:
            limitations.append(
                "Die eBay-Ergebnisseite enthielt keine lesbaren Angebote. Möglicherweise "
                "hat sich der Seitenaufbau geändert oder der Suchbegriff lieferte nichts."
            )
        if parse_errors:
            limitations.append(
                f"{parse_errors} eBay-Treffer konnten nicht gelesen werden."
            )

        # Weitergeblättert wird, solange die Seite voll war. Maßgeblich ist die
        # tatsächlich gelesene Kartenzahl (nicht die Anzahl lesbarer Angebote)
        # und die gemeldete Gesamttrefferzahl, wenn sie brauchbar ist.
        step = len(raw_items) or len(listings)
        has_next = step >= page_size
        if total is not None and query.offset + max(step, 0) >= total:
            has_next = False
        next_offset = query.offset + max(step, page_size) if has_next else None

        return SearchPage(
            listings=listings,
            offset=query.offset,
            limit=page_size,
            total_reported=total,
            has_next=has_next,
            next_offset=next_offset,
            truncated=bool(limitations),
            limitations=limitations,
            meta={
                "url": url,
                "page": page,
                "source": "ebay_web",
                "page_size": page_size,
                "cards": len(raw_items),
            },
        )

    # -- Parsing -----------------------------------------------------------

    def _to_raw_listing(
        self, item: dict[str, Any], *, fixed_price_only: bool = True
    ) -> RawListing | None:
        url = item.get("url")
        title = _clean_title(item.get("title"))
        if not url or not title:
            return None

        text = str(item.get("text") or "")
        subtitle = str(item.get("subtitle") or "")
        condition_text = None
        if subtitle:
            condition_text = subtitle.split("|")[0].strip() or None
        elif "Gebraucht" in text:
            condition_text = "Gebraucht"
        elif "Neu" in text:
            condition_text = "Neu"

        buying_options: list[str] = []
        platform_format: str | None = None
        if "Sofort-Kaufen" in text:
            buying_options.append("FIXED_PRICE")
            platform_format = "FIXED_PRICE"
        if "Auktion" in text or "Gebote" in text or "Auktionsende" in text:
            buying_options.append("AUCTION")
            if platform_format is None:
                platform_format = "AUCTION"
        if platform_format is None and fixed_price_only:
            # Die Suche wurde serverseitig auf Sofort-Kauf eingeschränkt
            # (LH_BIN=1); jedes Ergebnis ist damit ein Festpreisangebot.
            buying_options.append("FIXED_PRICE")
            platform_format = "FIXED_PRICE"

        feedback_match = _FEEDBACK_RE.search(text)
        seller_match = _SELLER_RE.search(text)

        listing_id = item.get("listing_id") or _item_id_from_url(str(url))
        return RawListing(
            platform=self.platform,
            platform_listing_id=str(listing_id) if listing_id else None,
            url=str(url),
            title=title,
            description=None,
            price=_parse_price(item.get("price")),
            currency="EUR",
            buying_options=buying_options,
            platform_format=platform_format,
            condition_text=condition_text,
            seller_name=seller_match.group(1) if seller_match else None,
            seller_feedback_percent=(
                float(feedback_match.group(1)) if feedback_match else None
            ),
            country="DE",
            image_url=item.get("image"),
            extra={"source": "ebay_web", "card_text": text[:400]},
        )

    # -- Diagnose ----------------------------------------------------------

    async def health_check(self) -> tuple[bool, str | None]:
        caps = self.capabilities
        if not caps.available:
            return False, caps.reason_unavailable
        return True, "Google Chrome wird für die eBay-Website-Suche gesteuert."

    async def test_connection(self) -> tuple[bool, str | None, dict[str, Any]]:
        caps = self.capabilities
        if not caps.available:
            return False, caps.reason_unavailable, {}
        query = SearchQuerySpec(
            term=self.probe_term,
            platform=self.platform,
            page_size=DEFAULT_PAGE_SIZE,
            max_pages=1,
            fixed_price_only=True,
        )
        try:
            page = await self.search_page(query)
        except ConnectorError as exc:
            return False, str(exc), {}
        blocked = bool(page.meta.get("blocked"))
        if blocked:
            return (
                False,
                "eBay hat den automatischen Abruf blockiert (Bot-Schutz). "
                "Bitte später erneut versuchen oder die Zugangsdaten-API nutzen.",
                {"blocked": True, "url": page.meta.get("url")},
            )
        if not page.listings:
            return (
                False,
                "Die eBay-Suchseite lieferte keine lesbaren Angebote. "
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
