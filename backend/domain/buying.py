"""Angebotsformat: Auktionen konsequent ausschließen (Abschnitt 7).

Grundsatz: Im Zweifel wird ein Angebot NICHT als Festpreisangebot behandelt.
Unbekanntes Format wird standardmäßig verworfen, nicht geraten.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..models import FORMAT_AUCTION, FORMAT_FIXED_PRICE, FORMAT_UNKNOWN

# eBay-BuyingOptions-Werte (Buy/Browse API)
EBAY_FIXED_PRICE = "FIXED_PRICE"
EBAY_AUCTION = "AUCTION"
EBAY_CLASSIFIED_AD = "CLASSIFIED_AD"

# Weitere Synonyme, die Connectoren liefern können
_FIXED_SYNONYMS = {
    "fixed_price",
    "fixed price",
    "sofortkauf",
    "sofort-kauf",
    "buy it now",
    "buy_it_now",
    "buyitnow",
    "bin",
    "festpreis",
    "fest preis",
    "preis vb",
    "vb",
}
_AUCTION_SYNONYMS = {
    "auction",
    "auktion",
    "steigern",
    "bieten",
    "gebot",
    "bid",
    "versteigerung",
}


@dataclass
class FormatDecision:
    """Ergebnis der Formatklassifizierung inkl. nachvollziehbarer Begründung."""

    format: str
    # True, wenn ein Festpreis als Option existiert, das Angebot aber
    # urspruenglich eine Auktion ist (Auktion mit Sofort-Kauf).
    mixed: bool = False
    # True, wenn ein separater, eindeutiger Festpreiswert vorliegt.
    fixed_price_available: bool = False
    evidence: str = ""
    notes: list[str] = field(default_factory=list)


def _normalize_option(option: object) -> str:
    return str(option or "").strip().lower()


def classify_format(
    *,
    buying_options: list[object] | None = None,
    platform_format: object | None = None,
    fixed_price: float | None = None,
    bid_count: int | None = None,
    current_bid: float | None = None,
) -> FormatDecision:
    """Bestimmt das Angebotsformat aus strukturierten Plattformdaten.

    Reihenfolge der Beweiskraft:
    1. Strukturierte buyingOptions der Plattform (eBay: FIXED_PRICE/AUCTION).
    2. Explizites Formatfeld des Connectors.
    3. Rueckfall-Heuristik über Gebotsdaten - reicht NUR für den
       Ausschluss, niemals für die Annahme als Festpreisangebot.
    """
    options = {_normalize_option(o) for o in (buying_options or []) if o}
    notes: list[str] = []

    has_fixed = bool(options & (_FIXED_SYNONYMS | {EBAY_FIXED_PRICE.lower()})) or any(
        "fixed" in o for o in options
    )
    has_auction = bool(options & (_AUCTION_SYNONYMS | {EBAY_AUCTION.lower()})) or any(
        "auction" in o or "auktion" in o for o in options
    )

    if has_auction and has_fixed:
        # Auktion mit Sofort-Kauf-Option. Der Festpreis ist nur dann
        # verwendbar, wenn ein separater, eindeutiger Wert vorliegt.
        if fixed_price is not None and fixed_price > 0:
            same_as_bid = (
                current_bid is not None and abs(float(fixed_price) - float(current_bid)) < 0.01
            )
            if same_as_bid:
                # Kein separater Festpreis - der Preis ist nur das aktuelle Gebot.
                return FormatDecision(
                    format=FORMAT_UNKNOWN,
                    mixed=True,
                    evidence="buyingOptions enthält Auktion und Festpreis, "
                    "aber der Preis entspricht dem aktuellen Gebot",
                    notes=["Preis ist nicht eindeutig als Festpreis belegbar"],
                )
            notes.append("Auktion mit Sofort-Kauf: Festpreis separat verfügbar")
            if bid_count:
                notes.append(f"Angebot hat bereits {bid_count} Gebot(e) - Bindung unklar")
            return FormatDecision(
                format=FORMAT_FIXED_PRICE,
                mixed=True,
                fixed_price_available=True,
                evidence="buyingOptions: AUCTION + FIXED_PRICE, separater Festpreis vorhanden",
                notes=notes,
            )
        return FormatDecision(
            format=FORMAT_UNKNOWN,
            mixed=True,
            evidence="buyingOptions enthält Auktion und Festpreis, kein separater Festpreiswert",
            notes=["Festpreis nicht separativ belegbar - standardmäßig ausgeschlossen"],
        )

    if has_auction:
        return FormatDecision(
            format=FORMAT_AUCTION,
            evidence="buyingOptions enthält Auktion",
            notes=["Auktion - nur Gebotspreis, kein Festpreis"],
        )

    if has_fixed:
        return FormatDecision(
            format=FORMAT_FIXED_PRICE,
            fixed_price_available=fixed_price is not None,
            evidence="buyingOptions enthält Festpreis (Sofort-Kauf)",
            notes=[] if fixed_price is not None else ["Festpreisformat ohne Preiswert"],
        )

    # Kein strukturiertes buyingOptions-Feld vorhanden -> Connector-Format prüfen.
    platform_value = _normalize_option(platform_format)
    if platform_value:
        if platform_value in _FIXED_SYNONYMS or platform_value == FORMAT_FIXED_PRICE:
            return FormatDecision(
                format=FORMAT_FIXED_PRICE,
                fixed_price_available=fixed_price is not None,
                evidence=f"Connector-Format: {platform_value}",
            )
        if platform_value in _AUCTION_SYNONYMS or platform_value == FORMAT_AUCTION:
            return FormatDecision(
                format=FORMAT_AUCTION, evidence=f"Connector-Format: {platform_value}"
            )

    # Letzter Rueckfall: Gebotsdaten belegen eine Auktion zweifelsfrei.
    if bid_count is not None and bid_count > 0:
        return FormatDecision(
            format=FORMAT_AUCTION,
            evidence=f"{bid_count} Gebot(e) vorhanden",
            notes=["Auktion über Gebotszahl erkannt"],
        )
    if current_bid is not None and fixed_price is None:
        return FormatDecision(
            format=FORMAT_AUCTION,
            evidence="nur aktueller Gebotspreis vorhanden",
            notes=["Preis ist ein Gebot, kein Festpreis"],
        )

    return FormatDecision(
        format=FORMAT_UNKNOWN,
        evidence="keine belastbaren Formatinformationen",
        notes=["Format unbekannt - standardmäßig ausgeschlossen"],
    )


def is_acceptable(
    decision: FormatDecision,
    *,
    exclude_auctions: bool = True,
    exclude_unknown: bool = True,
    allow_mixed: bool = True,
) -> tuple[bool, str | None]:
    """Prueft, ob ein Angebot nach den Formatregeln verwendet werden darf."""
    if decision.format == FORMAT_AUCTION:
        return (not exclude_auctions, "Auktion ausgeschlossen" if exclude_auctions else None)
    if decision.format == FORMAT_UNKNOWN:
        return (
            not exclude_unknown,
            "Angebotsformat unbekannt - ausgeschlossen" if exclude_unknown else None,
        )
    if decision.mixed and not allow_mixed:
        return False, "Auktion mit Sofort-Kauf ausgeschlossen"
    return True, None
