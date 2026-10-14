"""Kleinanzeigen-Connector (Abschnitt 6).

Rechercheergebnis (Stand der Prüfung):
* Kleinanzeigen betreibt KEINE oeffentliche API für Suche oder Angebotsdaten.
  Die eigenen Apps nutzen eine interne Schnittstelle, die nicht oeffentlich
  dokumentiert oder freigegeben ist.
* Die Nutzungsbedingungen untersagen automatisiertes Auslesen der Inhalte.

Konsequenz für dieses Projekt: Es wird bewusst KEIN Scraping, keine
Umgehung von Schutzmassnahmen und kein Zugriff über inoffizielle,
undokumentierte Endpunkte implementiert. Der Connector meldet stattdessen
ehrlich, dass keine zulässige automatische Suche verfügbar ist.

Austauschbarkeit: Diese Klasse erfuellt dieselbe Schnittstelle wie der
eBay-Connector. Sobald ein zulässiger Zugriffsweg existiert (offizielle API,
Partnerschnittstelle, eigener Datenexport, Feeds), genuegt eine neue
Implementierung von :class:`~backend.connectors.base.ListingConnector` mit
``platform = "kleinanzeigen"`` - Suchplanung, Bewertung und UI bleiben
unverändert.

Belege:
* Nutzungsbedingungen: https://themen.kleinanzeigen.de/nutzungsbedingungen/
* Oeffentliche Diskussion zur fehlenden API (interner Zugriff):
  https://stackoverflow.com/questions/43802316/
"""

from __future__ import annotations

import logging

from ..config import Settings
from .base import (
    ConnectorCapabilities,
    ConnectorUnavailableError,
    ListingConnector,
    RawListing,
    SearchPage,
    SearchQuerySpec,
)

logger = logging.getLogger(__name__)

UNVERIFIED_ACCESS_CATEGORIES: tuple[str, ...] = ("search", "pagination", "details", "images")

_LIMITATIONS = [
    "Kleinanzeigen stellt keine oeffentliche API für Suche oder Angebotsdaten bereit.",
    "Die Nutzungsbedingungen untersagen automatisiertes Auslesen von Inhalten.",
    "Es wurde bewusst keine Umgehung technischer Schutzmassnahmen implementiert.",
    "Ohne zulässige Schnittstelle kann diese Anwendung Kleinanzeigen nicht "
    "automatisch durchsuchen - die Abdeckung für diese Plattform ist daher 0 %.",
    "Ein zulässiger Weg sind eigene Datenexporte oder offizielle "
    "Partnerschnittstellen; der Connector ist dafür austauschbar ausgelegt.",
]


class KleinanzeigenConnector(ListingConnector):
    """Platzhalter-Connector mit ehrlicher Einschränkungsmeldung."""

    platform = "kleinanzeigen"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def capabilities(self) -> ConnectorCapabilities:
        return ConnectorCapabilities(
            platform=self.platform,
            label="Kleinanzeigen",
            enabled=bool(self.settings.kleinanzeigen_enabled),
            configured=False,
            available=False,
            supports_search=False,
            supports_pagination=False,
            supports_details=False,
            supports_server_side_fixed_price_filter=False,
            supports_price_filter=False,
            supports_condition_filter=False,
            supports_location_filter=False,
            supports_days_back_filter=False,
            supports_shipping_info=False,
            supports_pickup_info=False,
            provides_published_date=False,
            provides_seller_info=False,
            provides_images=False,
            page_size_max=None,
            max_results_per_query=0,
            max_results_per_result_set=0,
            limitations=list(_LIMITATIONS),
            docs_url="https://themen.kleinanzeigen.de/nutzungsbedingungen/",
            requires_credentials=False,
            setup_hint=(
                "Es existiert keine oeffentliche API. Zulässig sind eigene "
                "Exporte oder offizielle Partnerzugaenge - diese können als "
                "alternativer Connector angebunden werden."
            ),
            reason_unavailable=(
                "Keine oeffentliche, zulässige Schnittstelle verfügbar. "
                "Kleinanzeigen wird deshalb nicht automatisch durchsucht."
            ),
        )

    async def search_page(self, query: SearchQuerySpec) -> SearchPage:
        raise ConnectorUnavailableError(
            "Kleinanzeigen bietet keine oeffentliche API. Automatisiertes "
            "Auslesen ist nach den Nutzungsbedingungen nicht zulässig; es wird "
            "deshalb kein Zugriff implementiert. Bitte einen zulässigen "
            "Datenexport verwenden.",
            platform=self.platform,
        )

    async def fetch_details(self, platform_listing_id: str) -> RawListing | None:
        raise ConnectorUnavailableError(
            "Kleinanzeigen-Detailabruf ist ohne zulässige Schnittstelle nicht verfügbar.",
            platform=self.platform,
        )

    async def health_check(self) -> tuple[bool, str | None]:
        return False, self.capabilities.reason_unavailable

    async def test_connection(self) -> tuple[bool, str | None, dict]:
        return (
            False,
            self.capabilities.reason_unavailable,
            {
                "ergebnis": "Kein zulässiger automatischer Zugriffsweg.",
                "alternativen": [
                    "Eigene Suche im Browser und die Seite speichern; danach "
                    "scripts/import_html.py benutzen, um daraus echte Angebote "
                    "in die App zu laden.",
                ],
            },
        )
