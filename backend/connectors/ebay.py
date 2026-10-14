"""eBay-Connector auf Basis der offiziellen Browse API (Abschnitt 6).

Verwendete, zulässige Schnittstellen:
* OAuth2 Client-Credentials-Grant (Application Token)
  ``POST {base}/identity/v1/oauth2/token``
  Scope: ``https://api.ebay.com/oauth/api_scope``
* ``GET {base}/buy/browse/v1/item_summary/search`` - Produktsuche mit
  Filterung und Pagination.
* ``GET {base}/buy/browse/v1/item/{item_id}`` - Detailabruf (Beschreibung).

Wichtige, dokumentierte Eigenschaften der Browse API:

* Ohne ``filter=buyingOptions:...`` liefert die Suche nur Angebote, bei denen
  ``FIXED_PRICE`` (Sofort-Kauf) eine Kaufoption ist. Wir setzen den Filter
  zusätzlich explizit auf ``FIXED_PRICE`` - damit werden reine Auktionen
  serverseitig ausgeschlossen.
* ``limit`` max. 200, ``offset`` max. 9.999, max. 10.000 Ergebnisse pro
  Suchmenge.
* Es gibt KEIN serverseitiges Filterfeld für das Veröffentlichungsdatum.
  Der Zeitraumfilter wird deshalb lokal auf ``itemCreationDate`` angewendet.
  Diese Einschränkung wird ausgewiesen, nicht verschwiegen.
* ``itemSummary`` liefert Titel, Preis, Versandoptionen, Zustand, Verkäufer,
  Standort und ``itemCreationDate``. Die ausfuehrliche Beschreibung liefert
  nur der Detailabruf (``fieldgroups=EXTENDED`` ergänzt eine
  ``shortDescription``).
"""

from __future__ import annotations

import base64
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from ..config import Settings
from .base import (
    ConnectorAuthError,
    ConnectorCapabilities,
    ConnectorError,
    ConnectorRateLimitError,
    ConnectorUnavailableError,
    ListingConnector,
    RawListing,
    SearchPage,
    SearchQuerySpec,
)

logger = logging.getLogger(__name__)

BROWSE_SCOPE = "https://api.ebay.com/oauth/api_scope"
TOKEN_PATH = "/identity/v1/oauth2/token"
SEARCH_PATH = "/buy/browse/v1/item_summary/search"
ITEM_PATH = "/buy/browse/v1/item"

# Zustandsfilter der Browse API. Wird nur versucht; scheitert die Anfrage,
# läuft sie ohne diesen Filter erneut und der Zustand wird lokal geprüft.
CONDITION_FILTERS: dict[int, str] = {
    4: "NEW",
    3: "USED_EXCELLENT",
    2: "USED_GOOD",
    1: "USED_ACCEPTABLE",
    0: "FOR_PARTS_OR_NOT_WORKING",
}

# Ebay-Condition-IDs -> Klartext, falls 'condition' fehlt.
CONDITION_ID_LABELS: dict[int, str] = {
    1000: "Neu",
    1500: "Neu: Sonstige",
    1750: "Neu mit Mängeln",
    2000: "Vom Hersteller generalüberholt",
    2010: "Hervorragend - Refurbished",
    2020: "Sehr gut - Refurbished",
    2030: "Gut - Refurbished",
    2500: "Vom Verkäufer generalüberholt",
    3000: "Gebraucht",
    4000: "Sehr gut",
    5000: "Gut",
    6000: "Akzeptabel",
    7000: "Als Ersatzteil / defekt",
}


class EbayConnector(ListingConnector):
    platform = "ebay"
    # Suchbegriff für den Funktionstest ("Testen"-Knopf der Oberfläche).
    probe_term = "grafikkarte"

    def __init__(self, settings: Settings, *, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._client = client
        self._owns_client = client is None
        self._token: str | None = None
        self._token_expires_at: float = 0.0
        self._last_quota: dict[str, Any] = {}

    # -- Capabilities ------------------------------------------------------

    @property
    def capabilities(self) -> ConnectorCapabilities:
        configured = self.settings.ebay_configured
        caps = ConnectorCapabilities(
            platform=self.platform,
            label="eBay (API)",
            enabled=True,
            configured=configured,
            available=configured,
            supports_search=configured,
            supports_pagination=configured,
            supports_details=configured,
            supports_server_side_fixed_price_filter=configured,
            supports_price_filter=configured,
            # Der Zustandsfilter wird versucht, aber nicht garantiert: faellt
            # er weg, wird lokal gefiltert (siehe search_page).
            supports_condition_filter=False,
            supports_location_filter=configured,
            supports_days_back_filter=False,
            supports_shipping_info=configured,
            supports_pickup_info=configured,
            provides_published_date=configured,
            provides_seller_info=configured,
            provides_images=configured,
            page_size_max=200,
            max_results_per_query=self.settings.ebay_max_results_per_query,
            max_results_per_result_set=10_000,
            docs_url="https://developer.ebay.com/api-docs/buy/browse/overview.html",
            requires_credentials=True,
            setup_hint=(
                "Optional: App ID und Client Secret eines kostenlosen Production-Keysets "
                "in .env eintragen (EBAY_CLIENT_ID, EBAY_CLIENT_SECRET). Nicht nötig - "
                "fuer eBay ohne Zugangsdaten stattdessen 'eBay (Website)' auswaehlen."
            ),
            limitations=[
                "Kein serverseitiges Filterfeld für das Veröffentlichungsdatum - "
                "der Zeitraum wird lokal auf itemCreationDate angewendet.",
                "Maximal 10.000 Ergebnisse pro Suchergebnis-Menge; längere "
                "Trefferlisten sind nicht abrufbar.",
                "Angebotsbeschreibungen liefert die Suche nur als Kurzfassung "
                "(shortDescription); Volltexte erfordern einen Detailabruf.",
                "Der Zustandsfilter der Suche kann je nach Marktplatz abgelehnt "
                "werden; dann wird der Zustand lokal geprüft.",
            ],
        )
        if not configured:
            caps.reason_unavailable = (
                "Keine eBay-Zugangsdaten konfiguriert (EBAY_CLIENT_ID / "
                "EBAY_CLIENT_SECRET). Ohne diese ist keine Suche möglich."
            )
        return caps

    # -- HTTP --------------------------------------------------------------

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=6.0))
            self._owns_client = True
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed and self._owns_client:
            await self._client.aclose()
        self._client = None

    async def _get_token(self, *, force: bool = False) -> str:
        if not self.settings.ebay_configured:
            raise ConnectorAuthError(
                "eBay-Zugangsdaten fehlen (EBAY_CLIENT_ID / EBAY_CLIENT_SECRET).",
                platform=self.platform,
            )
        now = time.time()
        if not force and self._token and now < self._token_expires_at - 60:
            return self._token

        credentials = f"{self.settings.ebay_client_id}:{self.settings.ebay_client_secret}"
        encoded = base64.b64encode(credentials.encode("utf-8")).decode("ascii")
        client = await self._get_client()
        try:
            response = await client.post(
                f"{self.settings.ebay_api_base}{TOKEN_PATH}",
                headers={
                    "Authorization": f"Basic {encoded}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data={"grant_type": "client_credentials", "scope": BROWSE_SCOPE},
            )
        except httpx.HTTPError as exc:
            raise ConnectorUnavailableError(
                f"eBay-Authentifizierung nicht erreichbar: {exc}", platform=self.platform
            ) from exc

        if response.status_code == 401:
            raise ConnectorAuthError(
                "eBay hat die Zugangsdaten abgelehnt (HTTP 401). Client ID/Secret prüfen.",
                platform=self.platform,
            )
        if response.status_code >= 400:
            raise ConnectorError(
                f"eBay-Token-Anfrage fehlgeschlagen: HTTP {response.status_code} "
                f"{response.text[:200]}",
                platform=self.platform,
            )

        payload = response.json()
        token = payload.get("access_token")
        if not token:
            raise ConnectorError("eBay-Token-Antwort ohne access_token", platform=self.platform)
        self._token = token
        self._token_expires_at = now + float(payload.get("expires_in", 7200))
        return token

    # -- Suche -------------------------------------------------------------

    def _build_filter(self, query: SearchQuerySpec) -> tuple[str, bool]:
        """Baut den Browse-API-Filter. Rueckgabe: (filter, zustandsfilter_genutzt)."""
        parts: list[str] = []
        condition_used = False
        if query.fixed_price_only:
            parts.append("buyingOptions:{FIXED_PRICE}")

        if query.min_price is not None or query.max_price is not None:
            low = "" if query.min_price is None else _format_amount(query.min_price)
            high = "" if query.max_price is None else _format_amount(query.max_price)
            parts.append(f"price:[{low}..{high}]")
            parts.append("priceCurrency:EUR")

        if query.condition_texts:
            # Die API nimmt genau einen Zustand. Genommen wird der erste -
            # das ist der weite gebrauchte Zustand (siehe
            # ``normalize.searchable_condition_ranks``); die genaue Eingrenzung
            # (gut, sehr gut) prüft die Anwendung nach dem Abruf.
            for text in query.condition_texts:
                token = str(text).strip()
                if not token.isdigit():
                    continue
                condition = CONDITION_FILTERS.get(int(token))
                if condition:
                    parts.append(f"conditions:{{{condition}}}")
                    condition_used = True
                    break

        country = _marketplace_country(self.settings.ebay_marketplace_id)
        if country:
            parts.append(f"itemLocationCountry:{country}")

        if query.location and query.location.postal_code and country:
            parts.append(
                f"pickupPostalCode:{query.location.postal_code},"
                f"pickupCountry:{country},"
                f"pickupRadius:{int(query.location.radius_km)},"
                f"pickupRadiusUnit:km"
            )
        return ",".join(parts), condition_used

    def _headers(self, query: SearchQuerySpec) -> dict[str, str]:
        country = _marketplace_country(self.settings.ebay_marketplace_id) or "DE"
        # Ohne Standortangabe liefert eBay keine belastbaren Versandkosten.
        context = f"contextualLocation=country={country}"
        if query.location and query.location.postal_code:
            context += f",zip={query.location.postal_code}"
        return {
            "X-EBAY-C-MARKETPLACE-ID": self.settings.ebay_marketplace_id,
            "X-EBAY-C-ENDUSERCTX": context,
            "Accept": "application/json",
        }

    async def search_page(self, query: SearchQuerySpec) -> SearchPage:
        if not self.settings.ebay_configured:
            raise ConnectorUnavailableError(
                "eBay-Connector ist ohne Zugangsdaten nicht verfügbar.",
                platform=self.platform,
            )

        limit = max(1, min(query.page_size, 200))
        params: dict[str, Any] = {
            "q": query.term,
            "limit": limit,
            "offset": max(0, query.offset),
            "fieldgroups": "EXTENDED",
        }
        filter_value, condition_used = self._build_filter(query)
        if filter_value:
            params["filter"] = filter_value
        if query.days_back is not None and query.days_back <= 7:
            # Bei sehr kurzen Zeitraeumen sind die neuesten Angebote relevant.
            params["sort"] = "newlyListed"

        limitations: list[str] = []
        payload = await self._request(params, query)

        # Zustandsfilter kann marktplatzabhaengig abgelehnt werden: dann ohne
        # diesen Filter erneut versuchen und lokal prüfen.
        if payload is None and condition_used:
            limitations.append(
                "Der Zustandsfilter der eBay-Suche wurde abgelehnt - "
                "der Zustand wird lokal geprüft."
            )
            retry_params = dict(params)
            retry_params["filter"] = ",".join(
                part for part in filter_value.split(",") if not part.startswith("conditions:")
            )
            payload = await self._request(retry_params, query, allow_filter_failure=False)

        if payload is None:
            payload = {}

        items = payload.get("itemSummaries") or []
        listings: list[RawListing] = []
        parse_errors = 0
        for item in items:
            try:
                listing = self._to_raw_listing(item)
            except Exception as exc:  # Einzelner Treffer darf nichts abbrechen
                parse_errors += 1
                logger.warning("eBay-Angebot konnte nicht gelesen werden: %s", exc)
                continue
            if listing is not None:
                listings.append(listing)

        total = payload.get("total")
        offset = int(payload.get("offset", query.offset) or 0)
        returned = len(items)
        next_offset = offset + returned
        has_next = bool(payload.get("next")) or (
            isinstance(total, int) and next_offset < total and returned >= limit
        )
        if isinstance(total, int) and total > self.capabilities.max_results_per_result_set:
            limitations.append(
                f"eBay meldet {total} Treffer, liefert aber höchstens "
                f"{self.capabilities.max_results_per_result_set} pro Suchmenge."
            )
        if parse_errors:
            limitations.append(f"{parse_errors} eBay-Treffer konnten nicht gelesen werden.")

        return SearchPage(
            listings=listings,
            offset=offset,
            limit=limit,
            total_reported=total if isinstance(total, int) else None,
            has_next=has_next,
            next_offset=next_offset if has_next else None,
            truncated=bool(limitations),
            limitations=limitations,
            meta={"quota": dict(self._last_quota)},
        )

    async def _request(
        self,
        params: dict[str, Any],
        query: SearchQuerySpec,
        *,
        allow_filter_failure: bool = True,
        _retried_auth: bool = False,
    ) -> dict[str, Any] | None:
        token = await self._get_token()
        client = await self._get_client()
        try:
            response = await client.get(
                f"{self.settings.ebay_api_base}{SEARCH_PATH}",
                params=params,
                headers={**self._headers(query), "Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError as exc:
            raise ConnectorUnavailableError(
                f"eBay nicht erreichbar: {exc}", platform=self.platform, retryable=True
            ) from exc

        self._capture_quota(response)

        if response.status_code == 401 and not _retried_auth:
            self._token = None
            return await self._request(
                params, query, allow_filter_failure=allow_filter_failure, _retried_auth=True
            )
        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            raise ConnectorRateLimitError(
                "eBay-Zugriffslimit erreicht (HTTP 429).",
                platform=self.platform,
                retry_after=float(retry_after) if retry_after else None,
            )
        if response.status_code in (400, 422) and allow_filter_failure:
            logger.info("eBay lehnte Filter ab (HTTP %s) - Fallback ohne Filter",
                        response.status_code)
            return None
        if response.status_code >= 400:
            raise ConnectorError(
                f"eBay-Suche fehlgeschlagen: HTTP {response.status_code} "
                f"{response.text[:250]}",
                platform=self.platform,
                retryable=response.status_code >= 500,
            )
        try:
            return response.json()
        except ValueError as exc:
            raise ConnectorError(f"eBay-Antwort ist kein JSON: {exc}", platform=self.platform) from exc

    def _capture_quota(self, response: httpx.Response) -> None:
        quota = {}
        for header, key in (
            ("X-RateLimit-Limit", "limit"),
            ("X-RateLimit-Remaining", "remaining"),
            ("X-RateLimit-Reset", "reset"),
        ):
            value = response.headers.get(header)
            if value is not None:
                quota[key] = value
        if quota:
            self._last_quota = quota

    # -- Parsing -----------------------------------------------------------

    def _to_raw_listing(self, item: dict[str, Any]) -> RawListing | None:
        title = item.get("title")
        url = item.get("itemAffiliateWebUrl") or item.get("itemWebUrl")
        if not title or not url:
            return None

        price_container = item.get("price") or {}
        price = _to_float(price_container.get("value"))
        currency = price_container.get("currency") or "EUR"

        shipping_cost, shipping_known, pickup_available = self._shipping(item)

        location = item.get("itemLocation") or {}
        seller = item.get("seller") or {}
        image = item.get("image") or {}

        return RawListing(
            platform=self.platform,
            platform_listing_id=str(item.get("itemId")) if item.get("itemId") else None,
            url=url,
            title=str(title),
            description=item.get("shortDescription"),
            price=price,
            currency=currency,
            shipping_cost=shipping_cost,
            shipping_cost_known=shipping_known,
            pickup_available=pickup_available,
            buying_options=[str(o) for o in (item.get("buyingOptions") or [])],
            platform_format=None,
            bid_count=_to_int(item.get("bidCount")),
            current_bid=_to_float((item.get("currentBidPrice") or {}).get("value")),
            condition_text=item.get("condition")
            or CONDITION_ID_LABELS.get(_to_int(item.get("conditionId")) or -1),
            condition_id=str(item.get("conditionId")) if item.get("conditionId") else None,
            published_at=item.get("itemCreationDate"),
            seller_name=seller.get("username"),
            seller_feedback_percent=_to_float(seller.get("feedbackPercentage")),
            seller_feedback_score=_to_int(seller.get("feedbackScore")),
            location_text=location.get("city") or location.get("stateOrProvince"),
            postal_code=str(location.get("postalCode")) if location.get("postalCode") else None,
            country=location.get("country"),
            image_url=image.get("imageUrl") or _first_thumbnail(item),
            category_hint=item.get("categoryPath"),
            extra={
                "item_end_date": item.get("itemEndDate"),
                "legacy_item_id": item.get("legacyItemId"),
                "marketing_price": item.get("marketingPrice"),
                "categories": item.get("categories"),
            },
        )

    def _shipping(self, item: dict[str, Any]) -> tuple[float | None, bool, bool]:
        """Ermittelt Versandkosten. 'CALCULATED' bedeutet: Hoehe unbekannt."""
        options = item.get("shippingOptions") or []
        pickup_available = False
        costs: list[float] = []
        known = False
        for option in options:
            cost_type = str(option.get("shippingCostType") or "").upper()
            if cost_type == "FREE":
                costs.append(0.0)
                known = True
                continue
            if cost_type == "CALCULATED":
                # Hoehe ist erst nach Standortangabe bekannt.
                continue
            cost_container = option.get("shippingCost") or {}
            value = _to_float(cost_container.get("value"))
            if value is not None:
                costs.append(value)
                known = True
        for delivery in item.get("deliveryOptions") or []:
            if str(delivery).upper() in ("SELLER_ARRANGED_LOCAL_PICKUP", "LOCAL_PICKUP"):
                pickup_available = True
        if not options:
            # Keine Versandoptionen gemeldet -> typisch für reine Abholangebote.
            pickup_available = True
        return (min(costs) if costs else None, known, pickup_available)

    # -- Details -----------------------------------------------------------

    async def fetch_details(self, platform_listing_id: str) -> RawListing | None:
        if not self.settings.ebay_configured or not platform_listing_id:
            return None
        token = await self._get_token()
        client = await self._get_client()
        try:
            response = await client.get(
                f"{self.settings.ebay_api_base}{ITEM_PATH}/{platform_listing_id}",
                headers={
                    "X-EBAY-C-MARKETPLACE-ID": self.settings.ebay_marketplace_id,
                    "Accept": "application/json",
                    "Authorization": f"Bearer {token}",
                },
            )
        except httpx.HTTPError as exc:
            raise ConnectorUnavailableError(
                f"eBay-Detailabruf nicht erreichbar: {exc}", platform=self.platform
            ) from exc
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise ConnectorError(
                f"eBay-Detailabruf fehlgeschlagen: HTTP {response.status_code}",
                platform=self.platform,
            )
        item = response.json()
        raw = self._to_raw_listing(item)
        if raw is not None:
            # Volltext überschreibt die Kurzbeschreibung; HTML wird entfernt.
            raw.description = _strip_html(item.get("description")) or raw.description
        return raw

    async def health_check(self) -> tuple[bool, str | None]:
        caps = self.capabilities
        if not caps.configured:
            return False, caps.reason_unavailable
        try:
            await self._get_token()
            return True, None
        except ConnectorError as exc:
            return False, str(exc)

    async def test_connection(self) -> tuple[bool, str | None, dict[str, Any]]:
        """Echte Mini-Suche: prüft Token, Scope, Marktplatz und Trefferauswertung."""
        caps = self.capabilities
        if not caps.configured:
            return False, caps.reason_unavailable, {}
        probe = SearchQuerySpec(
            term=self.probe_term,
            platform=self.platform,
            page_size=1,
            max_pages=1,
            fixed_price_only=False,
        )
        try:
            page = await self.search_page(probe)
        except ConnectorAuthError as exc:
            return False, str(exc), {}
        except ConnectorError as exc:
            return False, str(exc), {}
        details: dict[str, Any] = {
            "ergebnis": f"eBay antwortet; {page.total_reported if page.total_reported is not None else page.count} Treffer für '{self.probe_term}'.",
            "treffer": page.total_reported if page.total_reported is not None else page.count,
            "marktplatz": self.settings.ebay_marketplace_id,
        }
        if page.meta.get("quota"):
            details["kontingent"] = page.meta["quota"]
        return True, None, details


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------


def _marketplace_country(marketplace_id: str) -> str | None:
    value = (marketplace_id or "").upper()
    if value.startswith("EBAY_"):
        suffix = value[5:]
        aliases = {
            "DE": "DE",
            "AT": "AT",
            "CH": "CH",
            "US": "US",
            "GB": "GB",
            "UK": "GB",
        }
        return aliases.get(suffix, suffix or None)
    return None


def _to_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _first_thumbnail(item: dict[str, Any]) -> str | None:
    thumbs = item.get("thumbnailImages") or []
    for thumb in thumbs:
        if isinstance(thumb, dict) and thumb.get("imageUrl"):
            return str(thumb["imageUrl"])
    return None


def _format_amount(value: float) -> str:
    # eBay erwartet Dezimalpunkt und maximal zwei Nachkommastellen.
    return f"{value:.2f}"


_HTML_TAG_RE = None


def _strip_html(value: Any) -> str | None:
    global _HTML_TAG_RE
    if not value:
        return None
    import re

    if _HTML_TAG_RE is None:
        _HTML_TAG_RE = re.compile(r"<[^>]+>")
    text = _HTML_TAG_RE.sub(" ", str(value))
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )
    return " ".join(text.split())


def days_ago(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)
