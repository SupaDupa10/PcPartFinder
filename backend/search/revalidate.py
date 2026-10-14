"""Bestandsprüfung: Was schon gespeichert ist, wird erneut beurteilt.

Warum es diesen Schritt gibt
----------------------------
Die Titelprüfung (``search.titles``) entscheidet, *ob* ein Angebot überhaupt
gespeichert wird. Sie wird mit jeder Verbesserung strenger - aber ein Angebot,
das eine frühere, schwächere Fassung durchgelassen hat, blieb bisher für immer
in der Ergebnisliste stehen. Genau so sammelt sich über die Zeit eine Liste aus
Kartons, Lüftern, Netzteilen, Server-Baugruppen und Suchgesuchen an, die mit
der eigentlichen Suche nichts zu tun hat.

Diese Prüfung geht deshalb über den **Bestand**: Jedes aktive Angebot wird mit
den aktuellen Regeln neu beurteilt. Was nicht mehr durchgeht, wird als
``rejected`` markiert (mit dem Grund) und aus der Ergebnisliste genommen. Der
Datensatz bleibt erhalten - die Entscheidung bleibt damit nachvollziehbar, und
eine versehentlich zu strenge Regel ist kein Datenverlust.

Zwei Dinge bleiben bewusst unangetastet:

* die **Merkliste** (``is_favorite``) - was der Benutzer selbst markiert hat,
  entscheidet er selbst;
* der **lokale Import** (``titles.SKIP_PLATFORMS``) - dort hat der Benutzer die
  Angebote ausgewählt.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..domain.price_limits import PriceList
from ..models import STATUS_ACTIVE, STATUS_REJECTED, Listing
from .titles import ART_LABELS, SKIP_PLATFORMS, match_product

logger = logging.getLogger(__name__)

MAX_EXAMPLES = 8


@dataclass
class RevalidationReport:
    """Ergebnis einer Bestandsprüfung (für Fortschritt, API und Protokoll)."""

    geprueft: int = 0
    aussortiert: int = 0
    geschont: int = 0
    # Angebote, die allein der Beschreibungstext dem Suchauftrag zuordnet.
    gerettet: int = 0
    nach_art: dict[str, int] = field(default_factory=dict)
    beispiele: list[dict] = field(default_factory=list)

    def zaehle(self, art: str) -> None:
        if art:
            self.nach_art[art] = self.nach_art.get(art, 0) + 1

    def to_dict(self) -> dict:
        return {
            "geprueft": self.geprueft,
            "aussortiert": self.aussortiert,
            "geschont": self.geschont,
            "gerettet": self.gerettet,
            "gruende": [
                {"art": art, "label": ART_LABELS.get(art, art), "anzahl": anzahl}
                for art, anzahl in sorted(self.nach_art.items(), key=lambda item: -item[1])
            ],
            "beispiele": list(self.beispiele),
        }

    def summary(self) -> str:
        """Kurzer, wahrheitsgemäßer Satz für die Oberfläche."""
        if self.geprueft == 0:
            return "Bestandsprüfung: es war nichts zu prüfen."
        gerettet = (
            f" {self.gerettet} davon wurden über die Beschreibung zugeordnet."
            if self.gerettet
            else ""
        )
        if self.aussortiert == 0:
            return (
                f"Bestandsprüfung: {self.geprueft} gespeicherte Angebote geprüft - "
                "alle passen weiterhin zum Suchauftrag." + gerettet
            )
        teile = ", ".join(
            f"{anzahl} {ART_LABELS.get(art, art)}"
            for art, anzahl in sorted(self.nach_art.items(), key=lambda item: -item[1])
            if anzahl
        )
        aufschluesselung = f" ({teile})" if teile else ""
        schonung = (
            f" {self.geschont} Angebote der Merkliste blieben unangetastet."
            if self.geschont
            else ""
        )
        return (
            f"Bestandsprüfung: {self.geprueft} gespeicherte Angebote geprüft, "
            f"{self.aussortiert} aus der Ergebnisliste genommen{aufschluesselung}. "
            "Der Grund steht jeweils am Angebot." + schonung + gerettet
        )


def revalidate_stored_listings(
    session: Session,
    *,
    price_list: PriceList | None = None,
    platform: str | None = None,
) -> RevalidationReport:
    """Beurteilt alle aktiven Angebote erneut und markiert die Aussortierten.

    ``price_list`` ist die Kauf-Limit-Liste: Ist sie vorhanden, muss ein Titel
    eine ihrer Komponenten nennen. Ohne Liste entscheiden die gespeicherten
    Merkmale des Angebots (Plattform, Kategorie, Modell).
    """
    report = RevalidationReport()
    stmt = select(Listing).where(Listing.status == STATUS_ACTIVE)
    if platform:
        stmt = stmt.where(Listing.platform == platform)
    listings = session.scalars(stmt.order_by(Listing.id.asc())).all()
    liste = price_list if (price_list is not None and price_list.available) else None

    for listing in listings:
        if listing.platform in SKIP_PLATFORMS:
            report.geschont += 1
            continue
        if listing.is_favorite:
            report.geschont += 1
            continue
        report.geprueft += 1
        # Dieselbe Prüfung wie im Suchlauf: Der Titel entscheidet, die
        # Beschreibung rettet nur die fehlende Produktzuordnung. Ohne diesen
        # Gleichlauf würde die Bestandsprüfung eine im Suchlauf gerettete
        # Anzeige beim nächsten Start wieder aussortieren.
        ergebnis, gerettet = match_product(
            listing.title,
            listing.description,
            term=listing.model or "",
            price_list=liste,
            category=listing.category,
        )
        if gerettet:
            report.gerettet += 1
        if ergebnis is None:
            continue
        art, grund = ergebnis
        listing.status = STATUS_REJECTED
        listing.reject_reason = grund[:256]
        warnings = [
            item
            for item in (listing.warnings or [])
            if not str(item).startswith("Bestandsprüfung:")
        ]
        warnings.append(f"Bestandsprüfung: {grund}")
        listing.warnings = warnings
        report.aussortiert += 1
        report.zaehle(art)
        if len(report.beispiele) < MAX_EXAMPLES:
            report.beispiele.append(
                {
                    "listing_id": listing.id,
                    "title": listing.title,
                    "art": art,
                    "art_label": ART_LABELS.get(art, art),
                    "reason": grund,
                }
            )

    if report.aussortiert:
        session.commit()
        logger.info("Bestandsprüfung: %s Angebote aussortiert", report.aussortiert)
    return report


__all__ = ["RevalidationReport", "revalidate_stored_listings"]
