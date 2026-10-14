"""Registry aller Plattform-Connectoren.

Die Auswahl der Plattformen in der UI steuert, welche Connectoren tatsächlich
verwendet werden. Nicht verfügbare Connectoren werden mit Begründung
gemeldet, statt still zu verschwinden.
"""

from __future__ import annotations

import logging

import httpx

from ..config import Settings
from .base import ListingConnector
from .ebay import EbayConnector
from .ebay_web import EbayWebConnector
from .kleinanzeigen import KleinanzeigenConnector
from .kleinanzeigen_web import KleinanzeigenWebConnector
from .local_import import LocalImportConnector

logger = logging.getLogger(__name__)

PLATFORM_LABELS: dict[str, str] = {
    "ebay": "eBay (API)",
    "ebay_web": "eBay (Website)",
    "kleinanzeigen": "Kleinanzeigen (ohne API)",
    "kleinanzeigen_web": "Kleinanzeigen (Website)",
    "local_import": "Lokaler Datenimport",
}


class ConnectorRegistry:
    def __init__(self, settings: Settings, *, http_client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._http_client = http_client
        self._connectors: dict[str, ListingConnector] = {}

    def build(self) -> dict[str, ListingConnector]:
        if self._connectors:
            return self._connectors
        self._connectors = {
            "ebay": EbayConnector(self.settings, client=self._http_client),
            "ebay_web": EbayWebConnector(self.settings),
            "kleinanzeigen": KleinanzeigenConnector(self.settings),
            "kleinanzeigen_web": KleinanzeigenWebConnector(self.settings),
            "local_import": LocalImportConnector(self.settings),
        }
        return self._connectors

    def get(self, platform: str) -> ListingConnector | None:
        return self.build().get(platform)

    def known_platforms(self) -> list[str]:
        return list(self.build().keys())

    def report(self) -> list[dict]:
        """Capability-Bericht für die UI (ehrlich und vollständig)."""
        return [connector.capabilities.to_dict() for connector in self.build().values()]

    def usable_platforms(self, requested: list[str]) -> tuple[list[str], list[dict]]:
        """Welche der gewuenschten Plattformen sind tatsächlich nutzbar?

        Rueckgabe: (nutzbare Plattformen, Meldungen zu nicht nutzbaren)
        """
        usable: list[str] = []
        notices: list[dict] = []
        connectors = self.build()
        for platform in requested:
            connector = connectors.get(platform)
            if connector is None:
                notices.append(
                    {
                        "platform": platform,
                        "code": "unknown_platform",
                        "message": f"Unbekannte Plattform '{platform}' - wird übersprungen.",
                    }
                )
                continue
            caps = connector.capabilities
            if caps.available and caps.supports_search:
                usable.append(platform)
            elif caps.platform == "ebay" and caps.enabled and not caps.configured:
                notices.append(
                    {
                        "platform": platform,
                        "code": "missing_credentials",
                        "message": (
                            "eBay (API) ist ausgewählt, aber es sind keine Zugangsdaten "
                            "konfiguriert. Der Suchlauf läuft für diese Plattform nicht - "
                            "die Abdeckung ist dort 0 %. Ohne Zugangsdaten einfach "
                            "'eBay (Website)' auswählen: das funktioniert ohne Konto."
                        ),
                        "hint": caps.setup_hint,
                    }
                )
            else:
                notices.append(
                    {
                        "platform": platform,
                        "code": "not_available",
                        "message": caps.reason_unavailable
                        or f"{caps.label} ist nicht verfügbar.",
                        "hint": caps.setup_hint,
                    }
                )
        # Doppelte eBay-Abdeckung vermeiden: Sind die offizielle API und die
        # Website-Variante beide nutzbar, hat die API Vorrang (stabiler, keine
        # doppelten Angebote in den Ergebnissen).
        if "ebay" in usable and "ebay_web" in usable:
            usable = [platform for platform in usable if platform != "ebay_web"]
            notices.append(
                {
                    "platform": "ebay_web",
                    "code": "superseded_by_api",
                    "message": (
                        "eBay liefert über die offizielle API - die Website-Suche "
                        "wird für diesen Lauf übersprungen, um Doppelte zu vermeiden."
                    ),
                }
            )
        return usable, notices

    async def aclose(self) -> None:
        for connector in self.build().values():
            await connector.aclose()
        if self._http_client is not None and not self._http_client.is_closed:
            await self._http_client.aclose()
        self._connectors = {}
