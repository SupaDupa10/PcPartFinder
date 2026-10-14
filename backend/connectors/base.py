"""Einheitliche Connector-Schnittstelle (Abschnitt 6).

Jeder Connector bietet:
* ``search``       - Suche ausfuehren
* ``search_page``  - eine Seite abrufen (Pagination)
* ``fetch_details``- Angebotsdetails abrufen, soweit zulässig
* ``capabilities`` - was tatsächlich unterstützt wird (ehrlich, ohne Wunschdenken)

Nicht unterstützte Funktionen werden als Einschränkung gemeldet, nicht
vorgetäuscht.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field, field_validator

from ..domain.normalize import parse_datetime


class ConnectorError(RuntimeError):
    """Allgemeiner Connector-Fehler (wird pro Anfrage abgefangen)."""

    def __init__(self, message: str, *, platform: str = "", retryable: bool = False) -> None:
        super().__init__(message)
        self.platform = platform
        self.retryable = retryable


class ConnectorUnavailableError(ConnectorError):
    """Quelle ist nicht verfügbar (keine zulässige Schnittstelle, deaktiviert)."""


class ConnectorAuthError(ConnectorError):
    """Zugangsdaten fehlen oder sind ungültig."""


class ConnectorRateLimitError(ConnectorError):
    """Zugriffslimit erreicht."""

    def __init__(self, message: str, *, platform: str = "", retry_after: float | None = None) -> None:
        super().__init__(message, platform=platform, retryable=True)
        self.retry_after = retry_after


class RawListing(BaseModel):
    """Connector-neutrales Angebot, vor der Normalisierung."""

    model_config = {"extra": "ignore"}

    platform: str
    platform_listing_id: str | None = None
    url: str
    title: str
    description: str | None = None

    price: float | None = None
    currency: str = "EUR"
    shipping_cost: float | None = None
    shipping_cost_known: bool = False
    pickup_available: bool = False

    # Strukturierte Formatinformationen (Abschnitt 7)
    buying_options: list[str] = Field(default_factory=list)
    platform_format: str | None = None
    bid_count: int | None = None
    current_bid: float | None = None

    condition_text: str | None = None
    condition_id: str | None = None

    published_at: datetime | None = None

    seller_name: str | None = None
    seller_feedback_percent: float | None = None
    seller_feedback_score: int | None = None

    location_text: str | None = None
    postal_code: str | None = None
    country: str | None = None

    image_url: str | None = None
    category_hint: str | None = None

    extra: dict[str, Any] = Field(default_factory=dict)

    @field_validator("published_at", mode="before")
    @classmethod
    def _parse_published(cls, value):
        if value is None or isinstance(value, datetime):
            return value
        return parse_datetime(value)

    @field_validator("url", "title", mode="before")
    @classmethod
    def _coerce_text(cls, value) -> str:
        return "" if value is None else str(value)

    @field_validator("buying_options", mode="before")
    @classmethod
    def _coerce_options(cls, value):
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        return [str(v) for v in value]


@dataclass
class LocationFilter:
    postal_code: str
    radius_km: float = 50.0


@dataclass
class SearchQuerySpec:
    """Eine konkrete Suchanfrage an eine Plattform."""

    term: str
    platform: str
    category: str | None = None
    family: str | None = None
    round_index: int = 1
    max_price: float | None = None
    min_price: float | None = None
    condition_texts: list[str] = field(default_factory=list)
    location: LocationFilter | None = None
    days_back: int | None = None
    page_size: int = 50
    max_pages: int = 1
    offset: int = 0
    sort: str | None = None
    exclusion_terms: list[str] = field(default_factory=list)
    fixed_price_only: bool = True
    # Reine Versandsuche: Quellen, die das serverseitig filtern können (der
    # Kleinanzeigen-Webconnector), schließen Abholangebote dann schon beim
    # Abruf aus. Nicht unterstützende Quellen ignorieren das Feld - die
    # Ingestion sortiert sie ohnehin aus.
    shipping_only: bool = False

    def key(self) -> str:
        return "|".join(
            [
                self.platform,
                self.term,
                str(self.max_price),
                str(self.min_price),
                str(self.location.postal_code if self.location else ""),
                str(self.location.radius_km if self.location else ""),
                str(self.days_back),
                str(self.round_index),
                str(self.sort or ""),
                str(self.shipping_only),
            ]
        )

    def with_offset(self, offset: int) -> "SearchQuerySpec":
        """Kopie der Anfrage mit anderem Offset (Pagination)."""
        return replace(self, offset=offset)


@dataclass
class SearchPage:
    """Ergebnis einer einzelnen Papierversion (Seite) einer Suchanfrage."""

    listings: list[RawListing] = field(default_factory=list)
    offset: int = 0
    limit: int = 0
    total_reported: int | None = None
    has_next: bool = False
    next_offset: int | None = None
    truncated: bool = False
    # Ehrliche Hinweise: was warum nicht abrufbar war.
    limitations: list[str] = field(default_factory=list)
    # Rohmetadaten, z. B. Antwortheader/Quota-Informationen.
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def count(self) -> int:
        return len(self.listings)


@dataclass
class ConnectorCapabilities:
    platform: str
    label: str
    enabled: bool = False
    configured: bool = False
    available: bool = False

    supports_search: bool = False
    supports_pagination: bool = False
    supports_details: bool = False
    supports_server_side_fixed_price_filter: bool = False
    supports_price_filter: bool = False
    supports_condition_filter: bool = False
    supports_location_filter: bool = False
    supports_days_back_filter: bool = False
    supports_shipping_info: bool = False
    supports_pickup_info: bool = False
    provides_published_date: bool = False
    provides_seller_info: bool = False
    provides_images: bool = False

    page_size_max: int | None = None
    max_results_per_query: int | None = None
    max_results_per_result_set: int | None = None

    limitations: list[str] = field(default_factory=list)
    docs_url: str | None = None
    requires_credentials: bool = False
    setup_hint: str | None = None
    reason_unavailable: str | None = None

    def to_dict(self) -> dict:
        return {
            "platform": self.platform,
            "label": self.label,
            "enabled": self.enabled,
            "configured": self.configured,
            "available": self.available,
            "supports": {
                "search": self.supports_search,
                "pagination": self.supports_pagination,
                "details": self.supports_details,
                "fixed_price_filter": self.supports_server_side_fixed_price_filter,
                "price_filter": self.supports_price_filter,
                "condition_filter": self.supports_condition_filter,
                "location_filter": self.supports_location_filter,
                "days_back_filter": self.supports_days_back_filter,
                "shipping_info": self.supports_shipping_info,
                "pickup_info": self.supports_pickup_info,
                "published_date": self.provides_published_date,
                "seller_info": self.provides_seller_info,
                "images": self.provides_images,
            },
            "page_size_max": self.page_size_max,
            "max_results_per_query": self.max_results_per_query,
            "max_results_per_result_set": self.max_results_per_result_set,
            "limitations": list(self.limitations),
            "docs_url": self.docs_url,
            "requires_credentials": self.requires_credentials,
            "setup_hint": self.setup_hint,
            "reason_unavailable": self.reason_unavailable,
        }


class ListingConnector(ABC):
    """Basisklasse aller Plattform-Connectoren."""

    platform: str = "unknown"

    # Zusätzliche Suchansichten (Sortierungen), die dieser Connector kennt.
    # Leer = nur die Standardsicht. Der Suchlauf nutzt eine zusätzliche Ansicht
    # bei breiten Suchbegriffen, weil die Plattform dort je Sortierung einen
    # anderen Ausschnitt der Treffer zeigt (z.B. eBay: Relevanz und "neueste
    # zuerst"). Die Standardsicht ist immer dabei und wird hier nicht gelistet.
    sort_variants: tuple[str, ...] = ()

    @property
    @abstractmethod
    def capabilities(self) -> ConnectorCapabilities:
        """Was dieser Connector tatsächlich kann und was nicht."""

    @abstractmethod
    async def search_page(self, query: SearchQuerySpec) -> SearchPage:
        """Eine Ergebnisseite abrufen."""

    async def search(self, query: SearchQuerySpec) -> list[RawListing]:
        """Alle Seiten bis zum Limit oder bis keine Ergebnisse mehr kommen."""
        results: list[RawListing] = []
        page_size = max(
            1,
            min(query.page_size, self.capabilities.page_size_max or query.page_size),
        )
        offset = query.offset
        for _page in range(max(1, query.max_pages)):
            page = await self.search_page(
                replace(query, page_size=page_size, offset=offset)
            )
            results.extend(page.listings)
            if not page.has_next or page.next_offset is None:
                break
            offset = page.next_offset
        return results

    async def fetch_details(self, platform_listing_id: str) -> RawListing | None:
        """Details abrufen. Standard: nicht unterstützt."""
        return None

    async def health_check(self) -> tuple[bool, str | None]:
        return self.capabilities.available, self.capabilities.reason_unavailable

    async def test_connection(self) -> tuple[bool, str | None, dict[str, Any]]:
        """Echter Funktionstest für die Oberfläche ("Testen"-Knopf).

        ``health_check`` prüft nur die Konfiguration. Hier darf ein Connector
        zusätzlich eine minimale echte Abfrage machen, damit der Benutzer
        sofort sieht, ob seine Zugangsdaten wirklich funktionieren.

        Rückgabe: (erfolgreich, Meldung, Details)
        """
        ok, reason = await self.health_check()
        return ok, reason, {}

    async def aclose(self) -> None:
        """Ressourcen freigeben."""
        return None


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
