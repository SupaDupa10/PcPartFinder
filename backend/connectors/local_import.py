"""Lokaler Import-Connector.

Zweck: Angebotsdaten einbinden, die der Benutzer selbst zulässig beschafft
hat (z. B. eigener Datenexport, Ergebnisse einer manuellen Recherche oder
eine offizielle Partner-Schnittstelle, die vorab in eine Datei geschrieben
wurde). Damit ist ein vollständiger Durchlauf der Pipeline auch ohne
eBay-Zugangsdaten möglich, ohne Zugriffsschutz zu umgehen.

Ehrlichkeitsprinzip: Importierte Angebote stammen NICHT aus einer Live-Suche.
Die Herkunft bleibt an jedem Angebot sichtbar (``platform = "local_import"``)
und wird in der Suchabdeckung getrennt ausgewiesen.

Dateiformat: JSON mit einer Liste oder ``{"listings": [...]}``.
Felder (alle optional ausser title/url):
  title, url, price, shipping_cost, currency, description, condition,
  published_at, postal_code, location, seller_name, seller_feedback_percent,
  image_url, platform_listing_id, buying_options, buying_options_text,
  pickup_available, shipping_cost_known
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from ..config import Settings
from ..domain.normalize import match_key_contains
from .base import (
    ConnectorCapabilities,
    ConnectorError,
    ListingConnector,
    RawListing,
    SearchPage,
    SearchQuerySpec,
)

logger = logging.getLogger(__name__)

DEFAULT_FILENAME = "local_import.json"


class LocalImportConnector(ListingConnector):
    platform = "local_import"

    def __init__(self, settings: Settings, *, path: str | None = None) -> None:
        self.settings = settings
        self.path = Path(path or getattr(settings, "local_import_path", "") or "")
        self._cache: list[RawListing] | None = None
        # Fingerabdruck der geladenen Datei: Aenderungen werden erkannt, ohne
        # die App neu zu starten. Beim manuellen Import (eigene Suche speichern,
        # umwandeln, Suche starten) ist das der entscheidende Komfortgewinn.
        self._cache_key: tuple[float, int] | None = None

    @property
    def configured(self) -> bool:
        return bool(str(self.path)) and self.path.name not in ("", ".") and self.path.is_file()

    @property
    def capabilities(self) -> ConnectorCapabilities:
        return ConnectorCapabilities(
            platform=self.platform,
            label="Lokaler Datenimport",
            enabled=True,
            configured=self.configured,
            available=self.configured,
            supports_search=True,
            supports_pagination=True,
            supports_details=False,
            supports_server_side_fixed_price_filter=False,
            supports_price_filter=True,
            supports_condition_filter=False,
            supports_location_filter=False,
            supports_days_back_filter=True,
            supports_shipping_info=True,
            supports_pickup_info=True,
            provides_published_date=True,
            provides_seller_info=True,
            provides_images=True,
            page_size_max=200,
            max_results_per_query=None,
            max_results_per_result_set=None,
            limitations=[
                "Keine Live-Suche: Es werden ausschließlich die vom Benutzer "
                "bereitgestellten Daten gelesen.",
                "Die Suche ist ein Textvergleich auf Titel und Beschreibung, "
                "keine Plattformsuche - die Abdeckung entspricht genau dem "
                "Inhalt der Importdatei.",
                "Filter (Format, Zustand, Zeitraum) werden lokal angewendet.",
            ],
            docs_url=None,
            requires_credentials=False,
            setup_hint=(
                "Pfad zur JSON-Importdatei in den Einstellungen setzen "
                "(LOCAL_IMPORT_PATH). Format siehe README."
            ),
            reason_unavailable=None if self.configured else "Keine Importdatei konfiguriert.",
        )

    # -- Laden -------------------------------------------------------------

    def _file_key(self) -> tuple[float, int] | None:
        """Fingerabdruck der Importdatei (Änderungszeit + Groesse)."""
        try:
            stat = self.path.stat()
        except OSError:
            return None
        return (stat.st_mtime, stat.st_size)

    def _load(self) -> list[RawListing]:
        key = self._file_key()
        if self._cache is not None and key is not None and key == self._cache_key:
            return self._cache
        if not self.configured:
            raise ConnectorError(
                f"Importdatei nicht gefunden: {self.path}", platform=self.platform
            )
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConnectorError(
                f"Importdatei konnte nicht gelesen werden: {exc}", platform=self.platform
            ) from exc

        raw_items = document.get("listings") if isinstance(document, dict) else document
        if not isinstance(raw_items, list):
            raise ConnectorError(
                "Importdatei muss eine Liste oder {'listings': [...]} enthalten.",
                platform=self.platform,
            )

        listings: list[RawListing] = []
        for index, item in enumerate(raw_items):
            if not isinstance(item, dict):
                continue
            try:
                listings.append(self._to_raw(item, index))
            except Exception as exc:
                logger.warning("Importeintrag %s übersprungen: %s", index, exc)
        self._cache = listings
        self._cache_key = key
        return listings

    def _to_raw(self, item: dict, index: int) -> RawListing:
        title = item.get("title") or item.get("titel")
        url = item.get("url") or item.get("link")
        if not title or not url:
            raise ValueError("Pflichtfelder title und url fehlen")
        options = item.get("buying_options") or item.get("buying_options_text")
        if isinstance(options, str):
            options = [part.strip() for part in options.split(",") if part.strip()]
        return RawListing(
            platform=self.platform,
            platform_listing_id=str(item.get("platform_listing_id") or item.get("id") or index),
            url=str(url),
            title=str(title),
            description=item.get("description") or item.get("beschreibung"),
            price=item.get("price") or item.get("preis"),
            currency=item.get("currency") or "EUR",
            shipping_cost=item.get("shipping_cost") or item.get("versand"),
            shipping_cost_known=bool(
                item.get("shipping_cost_known", item.get("shipping_cost") is not None)
            ),
            pickup_available=bool(item.get("pickup_available", False)),
            buying_options=options or [],
            platform_format=item.get("platform_format") or item.get("format"),
            condition_text=item.get("condition") or item.get("zustand"),
            published_at=item.get("published_at") or item.get("veröffentlicht"),
            seller_name=item.get("seller_name") or item.get("verkäufer"),
            seller_feedback_percent=item.get("seller_feedback_percent"),
            location_text=item.get("location") or item.get("ort"),
            postal_code=item.get("postal_code") or item.get("plz"),
            country=item.get("country", "DE"),
            image_url=item.get("image_url") or item.get("bild"),
            extra={"import_source": str(self.path.name)},
        )

    # -- Suche -------------------------------------------------------------

    async def search_page(self, query: SearchQuerySpec) -> SearchPage:
        all_listings = self._load()
        matched: list[RawListing] = []
        for listing in all_listings:
            haystack = f"{listing.title}\n{listing.description or ''}"
            if not match_key_contains(haystack, query.term, min_len=3):
                continue
            if listing.price is not None:
                if query.max_price is not None and listing.price > query.max_price:
                    continue
                # Mindestpreis der Suche (ein Viertel des Höchstpreises):
                # darunter liegt fast immer Zubehör statt des Produkts selbst.
                if query.min_price is not None and listing.price < query.min_price:
                    continue
            matched.append(listing)

        offset = max(0, query.offset)
        limit = max(1, query.page_size)
        window = matched[offset : offset + limit]
        next_offset = offset + len(window)
        has_next = next_offset < len(matched)
        return SearchPage(
            listings=window,
            offset=offset,
            limit=limit,
            total_reported=len(matched),
            has_next=has_next,
            next_offset=next_offset if has_next else None,
            truncated=False,
            limitations=[] if matched else [],
            meta={"source_file": str(self.path), "total_in_file": len(all_listings)},
        )

    async def health_check(self) -> tuple[bool, str | None]:
        if not self.configured:
            return False, f"Importdatei nicht gefunden: {self.path}"
        try:
            count = len(self._load())
        except ConnectorError as exc:
            return False, str(exc)
        return True, f"{count} Angebote in {self.path.name} importierbar"

    async def test_connection(self) -> tuple[bool, str | None, dict]:
        ok, message = await self.health_check()
        if not ok:
            return False, message, {}
        listings = self._load()
        with_price = [item for item in listings if item.price is not None]
        return (
            True,
            None,
            {
                "ergebnis": message,
                "angebote": len(listings),
                "mit_preis": len(with_price),
                "datei": str(self.path),
            },
        )
