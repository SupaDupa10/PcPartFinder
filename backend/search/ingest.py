"""Ingestion: von einem Roh-Angebot zum normalisierten Datenbankeintrag.

Reihenfolge (Abschnitt 14, Schritte 7-9 und 11):

1. Ausschlussbegriffe prüfen (lokal, nachvollziehbar).
2. Angebotsformat bestimmen und Auktionen ausschließen (Abschnitt 7).
3. Liefer-/Abholoptionen prüfen.
4. Preisfilter anwenden.
5. Preise, Zustand und Zeitpunkte normalisieren.
6. Hardwaremodell zuordnen.
7. Deduplizieren und speichern.

Jeder Fehler bei einem einzelnen Angebot wird abgefangen; ein defekter Treffer
bricht niemals den Suchlauf ab.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from ..connectors.base import RawListing
from ..domain import normalize
from ..domain.buying import FormatDecision, classify_format, is_acceptable
from ..domain.dedup import canonical_url
from ..domain.hardware import HardwareMatch, HardwareResolver
from ..domain.normalize import match_key_contains, tokenize
from ..domain.price_limits import PriceList, minimum_price
from ..knowledge.base import KnowledgeBase
from ..models import FORMAT_FIXED_PRICE
from ..scoring.risks import analyze_text_risk
from .titles import ART_TEIL, ART_VERPACKUNG, classify_title
from ..services.repository import (
    OUTCOME_NEW,
    OUTCOME_UNCHANGED,
    OUTCOME_UPDATED,
    ListingPayload,
    build_dedup_fields,
    upsert_listing,
)

logger = logging.getLogger(__name__)

# Ergebnisarten der Ingestion (werden zu Abdeckungszählern)
RESULT_USABLE = "usable"
RESULT_AUCTION = "auction"
RESULT_UNKNOWN_FORMAT = "unknown_format"
RESULT_PRICE = "price"
RESULT_EXCLUSION_TERM = "exclusion_term"
RESULT_RELEVANCE = "relevance"
RESULT_LOCATION = "location"
RESULT_ERROR = "error"
# Der Titel wurde dem gesuchten Produkt nicht zugeordnet (Titelprüfung).
# Solche Treffer werden gar nicht erst gespeichert.
RESULT_TITLE_FILTER = "title_filter"
# Zustand "Defekt / Ersatzteil" bzw. "Neu / ungeöffnet": Beides gehört nicht in
# eine Gebraucht-Suche nach funktionierender Hardware und wird verworfen.
RESULT_DEFECT = "defect"
RESULT_NEW_ITEM = "new_item"

# Quellen, bei denen nicht gefiltert wird: Der lokale Import enthält Angebote,
# die der Benutzer selbst ausgewählt hat (siehe ``search.titles.SKIP_PLATFORMS``).
SKIP_FILTER_PLATFORMS: frozenset[str] = frozenset({"local_import"})

# Risikosignale, die ein Angebot eindeutig als defekt oder als Ersatzteil
# ausweisen (Codes aus ``scoring.risks``). Absichtlich *nur* die harten Signale:
# "keine Garantie", "ungetestet", "Kratzer" oder "Spulenfiepen" sind keine
# Defekte und dürfen ein echtes Angebot nicht verwerfen.
HARD_DEFECT_CODES: frozenset[str] = frozenset(
    {
        "defect",  # defekt
        "for_parts",  # Ersatzteil
        "not_working",  # funktioniert nicht
        "not_functional",  # nicht funktionsfähig
        "does_not_run",  # läuft nicht
        "does_not_start",  # startet nicht
        "no_display_output",  # gibt kein Bild
        "no_signal",  # kein Bildsignal
        "fan_defect",  # Lüfter defekt
        "water_damage",  # Wasserschaden
        "display_damage",  # Displaybruch
        "broken_pins",  # abgebrochene Pins
        "bent_pins",  # verbogene Pins
        "oxidized",  # oxidiert
        "cracked",  # Riss
        "bastler",  # Bastlerartikel
    }
)


def defect_or_new_reason(
    title: str | None,
    description: str | None = None,
    *,
    condition_text: str | None = None,
    condition_id: object | None = None,
) -> tuple[str, str] | None:
    """Erkennt Angebote, die defekt sind oder Neuware anbieten.

    Rückgabe: ``(Ergebnisart, Begründung)`` oder ``None``.

    Zwei Stufen, beide rein deterministisch (ohne KI):

    1. Der **Zustand** des Angebots (eBay liefert dafür eigene Werte wie "Als
       Ersatzteil / defekt" oder "Neu"). Das ist das zuverlässigste Signal.
    2. Der **Text** (Titel und Beschreibung) über die harten Risikosignale.

    Die Suche ist auf gebrauchte, funktionierende Hardware ausgelegt: Ein
    defektes Angebot oder eine Neuware-Anzeige ist kein Deal, sondern
    Ausschuss für diese Liste.
    """
    rank = normalize.resolve_condition_rank(condition_text, condition_id)
    if rank == normalize.CONDITION_DEFECT:
        return (
            RESULT_DEFECT,
            f"Zustand '{condition_text or 'defekt/Ersatzteil'}': defekte oder zerlegte Ware",
        )
    if rank == normalize.CONDITION_NEW:
        return (
            RESULT_NEW_ITEM,
            f"Zustand '{condition_text or 'neu'}': Neuware statt Gebrauchtware",
        )

    for signal in analyze_text_risk(title, description).negatives:
        if signal.code in HARD_DEFECT_CODES:
            return RESULT_DEFECT, f"Anzeigentext weist auf einen Defekt hin ({signal.label})"
    return None

def packaging_only_reason(title: str | None) -> str | None:
    """Erkennt Titel, die offensichtlich nur Verpackung, Papiere oder ein Teil anbieten.

    Rückgabe ``None`` heißt: Der Titel ist kein solcher Fall. Die Regeln stehen
    in ``titles.classify_title`` - also an *einer* Stelle, damit Vorprüfung und
    Titelprüfung nicht auseinanderlaufen. Die Prüfung ist eng gefasst, damit kein
    echtes Angebot verloren geht: "mit OVP", "inkl. OVP" oder "Rechnung dabei"
    sind Eigenschaften eines echten Angebots und schließen nicht aus.
    """
    if not (title or "").strip():
        return None
    ergebnis = classify_title(title)
    if ergebnis is None:
        return None
    art, grund = ergebnis
    if art in (ART_VERPACKUNG, ART_TEIL):
        return grund
    return None


@dataclass
class IngestOptions:
    max_price: float | None = None
    # Kauf-Limits aus "Höchstpreise.txt". Ist die Liste geladen, wird jedes
    # Angebot zusätzlich an dem Limit seiner Komponente gemessen - unabhängig
    # davon, über welche Suchanfrage es hereinkam.
    price_list: PriceList | None = None
    # Mindestpreis des Suchauftrags. Wird aus dem Höchstpreis abgeleitet (ein
    # Viertel davon), sofern er nicht ausdrücklich gesetzt wird. Je Anfrage kann
    # er über ``ingest(..., min_price=...)`` genauer gesetzt werden; sonst gilt
    # dieser Wert.
    min_price: float | None = None
    delivery: str = "both"
    postal_code: str | None = None
    radius_km: float | None = None
    days_back: int | None = None
    exclude_auctions: bool = True
    exclude_unknown_format: bool = True
    allow_mixed_auction_bin: bool = True
    exclusion_terms: list[str] = field(default_factory=list)
    exclude_problematic: bool = True
    category_hint: str | None = None
    # Relevanzfilter für die Suche nach einem konkreten Produktnamen: Ein
    # Angebot muss mindestens ``required_min_matches`` dieser Bestandteile in
    # Titel oder Beschreibung nennen.
    required_tokens: list[str] = field(default_factory=list)
    required_min_matches: int = 0

    def __post_init__(self) -> None:
        # Der Mindestpreis gehört zum Höchstpreis: Ohne ihn würde die Ingestion
        # sehr günstige Angebote (Zubehör, Einzelteile, defekte Ware) als
        # Treffer speichern. Er wird deshalb hier abgeleitet, nicht an jeder
        # Aufrufstelle wiederholt.
        if self.min_price is None:
            self.min_price = minimum_price(self.max_price)


@dataclass
class IngestOutcome:
    result: str
    reason: str | None = None
    listing_id: int | None = None
    outcome: str | None = None  # new | updated | unchanged
    format_decision: FormatDecision | None = None
    hardware: HardwareMatch | None = None
    payload: ListingPayload | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def stored(self) -> bool:
        return self.outcome is not None


@dataclass
class _BaseChecks:
    """Ergebnis der deterministischen Prüfungen vor dem Speichern.

    ``outcome`` gesetzt heißt: Das Angebot ist schon hier ausgeschlossen und
    wird nicht weiter betrachtet. Sonst stehen die normalisierten Werte bereit,
    die die weiteren Schritte brauchen.
    """

    decision: FormatDecision | None = None
    price: float | None = None
    shipping_cost: float | None = None
    total: float | None = None
    distance_km: float | None = None
    outcome: IngestOutcome | None = None


class OfferIngestor:
    """Verarbeitet Roh-Angebote zu gespeicherten, bewertbaren Angeboten."""

    def __init__(
        self,
        session: Session,
        *,
        knowledge: KnowledgeBase,
        resolver: HardwareResolver,
        options: IngestOptions,
    ) -> None:
        self.session = session
        self.knowledge = knowledge
        self.resolver = resolver
        self.options = options

    # -- Hauptpfad ---------------------------------------------------------

    def ingest(
        self,
        raw: RawListing,
        *,
        run_id: int | None = None,
        exclusion_terms: list[str] | None = None,
        min_price: float | None = None,
    ) -> IngestOutcome:
        """Verarbeitet ein Roh-Angebot.

        ``exclusion_terms`` ergänzt die konfigurierten Ausschlussbegriffe um
        die Begriffe der konkreten Suchanfrage; ``min_price`` setzt den
        Mindestpreis dieser Anfrage. Beides erfolgt bewusst pro Aufruf und
        nicht über die gemeinsame Instanz, damit parallele Suchanfragen sich
        nicht gegenseitig beeinflussen.
        """
        try:
            return self._ingest(
                raw,
                run_id=run_id,
                exclusion_terms=exclusion_terms,
                min_price=min_price,
            )
        except Exception as exc:  # Einzelfehler darf den Lauf nicht stoppen
            logger.warning("Angebot konnte nicht verarbeitet werden: %s", exc, exc_info=False)
            return IngestOutcome(result=RESULT_ERROR, reason=f"{type(exc).__name__}: {exc}")

    def precheck(
        self,
        raw: RawListing,
        *,
        exclusion_terms: list[str] | None = None,
        min_price: float | None = None,
    ) -> IngestOutcome | None:
        """Dieselben Prüfungen wie ``_ingest`` (Schritte 1, 3-5) - ohne Speichern.

        Wird **vor** der Titelprüfung aufgerufen. Was hier schon ausgeschlossen
        wird (Ausschlussbegriff, Auktionsformat, Preis, Standort, Zeitraum),
        muss die Titelprüfung nicht mehr beurteilen: Das hält sie auf das
        Wesentliche beschränkt.

        Zusätzlich läuft hier die Titelprüfung für die *eindeutigen* Fälle
        (``title_screen``): Ein Titel, der nur Verpackung oder Papiere anbietet,
        wird schon hier verworfen. Alles Übrige (Zubehör, Einzelteile, fremde
        Modelle) entscheidet in der Suche die Titelprüfung, deshalb läuft der
        Produktabgleich (Schritt 2) hier weiterhin nicht.
        """
        return self._deterministic_checks(
            raw,
            exclusion_terms=exclusion_terms,
            relevance=False,
            title_screen=True,
            min_price=min_price,
        ).outcome

    def _deterministic_checks(
        self,
        raw: RawListing,
        *,
        exclusion_terms: list[str] | None,
        relevance: bool,
        title_screen: bool = False,
        min_price: float | None = None,
    ) -> _BaseChecks:
        """Ausschlussbegriffe, Format, Preis/Standort/Zeitraum (Schritte 1-5)."""
        checks = _BaseChecks()
        text = f"{raw.title}\n{raw.description or ''}"

        # 1. Ausschlussbegriffe (konfiguriert + anfragespezifisch)
        terms = list(dict.fromkeys([*self.options.exclusion_terms, *(exclusion_terms or [])]))
        for term in terms:
            if match_key_contains(text, term, min_len=4):
                checks.outcome = IngestOutcome(
                    result=RESULT_EXCLUSION_TERM,
                    reason=f"Ausschlussbegriff '{term}' im Angebot gefunden",
                )
                return checks

        # 1b. Eindeutige Verpackungs- und Papierangebote.
        if title_screen:
            verpackung = packaging_only_reason(raw.title)
            if verpackung is not None:
                checks.outcome = IngestOutcome(
                    result=RESULT_TITLE_FILTER, reason=verpackung
                )
                return checks

        # 1c. Defekte und neue Ware (gilt immer). Die Suche ist auf
        #     gebrauchte, funktionierende Hardware ausgelegt - und selbst dann,
        #     wenn die Titelprüfung ausfällt, darf das nicht durchrutschen.
        if raw.platform not in SKIP_FILTER_PLATFORMS:
            verdict = defect_or_new_reason(
                raw.title,
                raw.description,
                condition_text=raw.condition_text,
                condition_id=raw.condition_id,
            )
            if verdict is not None:
                result, reason = verdict
                checks.outcome = IngestOutcome(result=result, reason=reason)
                return checks

        # 2. Relevanz für einen konkreten Produktnamen (falls vorgegeben).
        if (
            relevance
            and self.options.required_tokens
            and self.options.required_min_matches > 0
        ):
            reason = self._check_relevance(text)
            if reason is not None:
                checks.outcome = IngestOutcome(result=RESULT_RELEVANCE, reason=reason)
                return checks

        # 3. Angebotsformat (Abschnitt 7)
        decision = classify_format(
            buying_options=raw.buying_options,
            platform_format=raw.platform_format,
            fixed_price=raw.price,
            bid_count=raw.bid_count,
            current_bid=raw.current_bid,
        )
        checks.decision = decision
        acceptable, reason = is_acceptable(
            decision,
            exclude_auctions=self.options.exclude_auctions,
            exclude_unknown=self.options.exclude_unknown_format,
            allow_mixed=self.options.allow_mixed_auction_bin,
        )
        if not acceptable:
            result = (
                RESULT_AUCTION
                if decision.format == "auction"
                else RESULT_UNKNOWN_FORMAT
                if decision.format == "unknown"
                else RESULT_AUCTION
            )
            checks.outcome = IngestOutcome(
                result=result, reason=reason, format_decision=decision
            )
            return checks

        # 4. Preis und Lieferung normalisieren
        price = normalize.round_money(raw.price)
        shipping_cost = normalize.round_money(raw.shipping_cost)
        total = normalize.compute_total_price(
            price, shipping_cost, raw.shipping_cost_known, raw.pickup_available
        )
        checks.price = price
        checks.shipping_cost = shipping_cost
        checks.total = total

        location_outcome, distance_km = self._check_location(raw)
        checks.distance_km = distance_km
        if location_outcome is not None:
            checks.outcome = IngestOutcome(
                result=RESULT_LOCATION,
                reason=location_outcome,
                format_decision=decision,
            )
            return checks

        if not self._within_budget(price, total, raw.shipping_cost_known):
            limit = self.options.max_price
            checks.outcome = IngestOutcome(
                result=RESULT_PRICE,
                reason=f"Preis über dem Limit von {limit:.2f} EUR",
                format_decision=decision,
            )
            return checks

        # 4b. Mindestpreis: ein Viertel des Höchstpreises. Sehr günstige Angebote
        #     sind Zubehör, Einzelteile oder defekte Ware und damit kein Treffer
        #     zum gesuchten Produkt.
        minimum = min_price if min_price is not None else self.options.min_price
        if not self._above_minimum(price, total, raw.shipping_cost_known, minimum):
            checks.outcome = IngestOutcome(
                result=RESULT_PRICE,
                reason=(
                    f"Preis unter dem Mindestpreis von {minimum:.2f} EUR "
                    "(ein Viertel des Höchstpreises)"
                ),
                format_decision=decision,
            )
            return checks

        # 5. Zeitraum (das Veröffentlichungsdatum steht direkt am Angebot)
        if raw.published_at is not None and not self._within_period(raw.published_at):
            checks.outcome = IngestOutcome(
                result=RESULT_PRICE,
                reason="Veröffentlichung liegt außerhalb des gewählten Zeitraums",
                format_decision=decision,
            )
        return checks

    def _ingest(
        self,
        raw: RawListing,
        *,
        run_id: int | None,
        exclusion_terms: list[str] | None = None,
        min_price: float | None = None,
    ) -> IngestOutcome:
        checks = self._deterministic_checks(
            raw, exclusion_terms=exclusion_terms, relevance=True, min_price=min_price
        )
        if checks.outcome is not None:
            return checks.outcome

        # 5. Zustand und Zeit
        decision = checks.decision
        price = checks.price
        shipping_cost = checks.shipping_cost
        total = checks.total
        distance_km = checks.distance_km
        assert decision is not None  # von _deterministic_checks gesetzt
        condition_rank = normalize.resolve_condition_rank(raw.condition_text, raw.condition_id)
        published_at = raw.published_at
        published_known = published_at is not None

        # 6. Hardwaremodell
        hardware = self.resolver.resolve(
            raw.title, raw.description, category_hint=raw.category_hint or self.options.category_hint
        )

        # 6b. Kauf-Limit der Komponente: Was über dem Limit liegt, lohnt sich
        #     nicht und wird verworfen statt nur markiert. So erscheint es
        #     weder in der Ergebnisliste noch in der Abdeckung als Treffer.
        if self.options.price_list is not None:
            limit = self.options.price_list.limit_for(
                title=raw.title, model=hardware.model, variant=hardware.variant
            )
            if limit is not None:
                value = total if total is not None else price
                if value is not None and value > limit.max_price:
                    return IngestOutcome(
                        result=RESULT_PRICE,
                        reason=(
                            f"Über dem Kauf-Limit für {limit.name} "
                            f"({limit.max_price:.0f} €): {value:.2f} €"
                        ),
                        format_decision=decision,
                        hardware=hardware,
                    )

        warnings: list[str] = []
        if hardware.notes:
            warnings.extend(hardware.notes)
        if decision.notes:
            warnings.extend(decision.notes)
        if published_at is None:
            warnings.append("Veröffentlichungszeitpunkt unbekannt")
        if not raw.shipping_cost_known:
            warnings.append("Versandkosten nicht angegeben")

        canonical, dedup = build_dedup_fields(
            platform=raw.platform,
            url=raw.url,
            title=raw.title,
            total_price=total,
            seller_name=raw.seller_name,
        )
        payload = ListingPayload(
            platform=raw.platform,
            platform_listing_id=raw.platform_listing_id,
            url=raw.url,
            canonical_url=canonical,
            dedup_fingerprint=dedup,
            title=raw.title.strip(),
            description=(raw.description or "").strip() or None,
            price=price,
            shipping_cost=shipping_cost,
            shipping_cost_known=raw.shipping_cost_known,
            total_price=total,
            currency=raw.currency or "EUR",
            published_at=published_at,
            published_at_known=published_known,
            seller_name=raw.seller_name,
            seller_feedback_percent=raw.seller_feedback_percent,
            seller_feedback_score=raw.seller_feedback_score,
            location_text=raw.location_text,
            postal_code=normalize.normalize_postal_code(raw.postal_code),
            country=raw.country,
            condition_text=raw.condition_text,
            condition_rank=condition_rank,
            category=hardware.category,
            manufacturer=hardware.manufacturer,
            product_family=hardware.product_family,
            model=hardware.model,
            variant=hardware.variant,
            model_candidates=[c.to_dict() for c in hardware.candidates] or None,
            listing_format=decision.format,
            format_evidence=decision.evidence,
            pickup_available=bool(raw.pickup_available),
            image_url=raw.image_url,
            raw={"extra": raw.extra, "buying_options": raw.buying_options},
        )

        if (
            self.options.exclude_problematic
            and payload.model
            and self._is_problematic(payload.category, payload.model)
        ):
            return IngestOutcome(
                result=RESULT_EXCLUSION_TERM,
                reason=f"Modell '{payload.model}' ist in der Wissensbasis als problematisch markiert",
                format_decision=decision,
                hardware=hardware,
                payload=payload,
            )

        content_hash = normalize.content_hash(
            payload.platform,
            payload.platform_listing_id or payload.canonical_url,
            payload.title,
            payload.description,
            str(payload.total_price),
            str(payload.condition_rank),
            payload.model,
            payload.variant,
            payload.status,
        )

        listing, outcome = upsert_listing(
            self.session, payload, run_id=run_id, content_hash=content_hash
        )
        if distance_km is not None:
            warnings.append(f"Abholung grob geschätzt {distance_km:.0f} km entfernt")

        return IngestOutcome(
            result=RESULT_USABLE,
            listing_id=listing.id,
            outcome=outcome,
            format_decision=decision,
            hardware=hardware,
            payload=payload,
            warnings=warnings,
        )

    # -- Einzelpruefungen --------------------------------------------------

    def _within_budget(
        self, price: float | None, total: float | None, shipping_known: bool
    ) -> bool:
        limit = self.options.max_price
        if limit is None:
            return True
        if price is None:
            # Ohne Preis ist kein belastbarer Vergleich möglich.
            return False
        # Bevorzugt wird der Gesamtpreis verglichen; ist die Versandhoehe
        # unbekannt, wird der Artikelpreis gegen das Limit geprüft und das
        # Scoring weist die Unsicherheit aus.
        if shipping_known and total is not None:
            return total <= limit
        return price <= limit

    def _above_minimum(
        self,
        price: float | None,
        total: float | None,
        shipping_known: bool,
        minimum: float | None,
    ) -> bool:
        """True, wenn der Preis den Mindestpreis erreicht (oder keiner gilt).

        Verglichen wird - wie beim Höchstpreis - der Gesamtpreis, sofern die
        Versandhöhe bekannt ist. Ohne Mindestpreis oder ohne Preis ist keine
        belastbare Aussage möglich: Dann wird das Angebot nicht *deshalb*
        verworfen (ein fehlender Preis fällt über den Höchstpreis-Filter
        heraus, sofern einer gilt).
        """
        if minimum is None or price is None:
            return True
        value = total if shipping_known and total is not None else price
        return value >= minimum

    def _within_period(self, published_at) -> bool:
        days = self.options.days_back
        if days is None:
            return True
        age = normalize.age_days(published_at)
        if age is None:
            return True
        return age <= days

    def _check_relevance(self, text: str) -> str | None:
        """Prüft, ob ein Angebot überhaupt zum gesuchten Produkt gehört.

        Die breiten Kategoriebegriffe im Suchplan liefern bewusst auch
        Angebote anderer Produkte. Ohne diesen Filter stünde in der
        Ergebnisliste alles, was die Plattform zur Kategorie ausspuckt.
        """
        text_tokens = set(normalize.tokenize(text))
        compact = "".join(normalize.tokenize(text))
        matched = 0
        for token in self.options.required_tokens:
            if token in text_tokens:
                matched += 1
                continue
            # Auch zusammengeschriebene Schreibweisen zählen ("rtx3080").
            if len(token) >= 3 and token in compact:
                matched += 1
        if matched >= self.options.required_min_matches:
            return None
        return (
            f"Gehört nicht zum gesuchten Produkt (nur {matched} von "
            f"{self.options.required_min_matches} erforderlichen Bestandteilen "
            f"im Angebotstext)"
        )

    def _check_location(self, raw: RawListing) -> tuple[str | None, float | None]:
        mode = self.options.delivery
        if mode == "shipping":
            if raw.pickup_available and not raw.shipping_cost_known:
                return ("Angebot ist nur zur Abholung verfügbar", None)
            return (None, None)

        postal_code = normalize.normalize_postal_code(raw.postal_code)
        distance = (
            normalize.postcode_distance_km(self.options.postal_code, postal_code)
            if postal_code
            else None
        )

        if mode == "pickup":
            if postal_code is None:
                return (
                    "Abholung gewünscht, aber kein verwertbarer Verkäuferstandort angegeben",
                    None,
                )
            if distance is None:
                # Keine belastbare Entfernung -> nicht erfinden, aber auch nicht
                # verwerfen; das Scoring weist die Unsicherheit aus.
                return (None, None)
            if self.options.radius_km is not None:
                # Grobe Näherung, daher mit Toleranz.
                if distance > self.options.radius_km * 1.5:
                    return (
                        f"Grob geschätzte Entfernung ({distance:.0f} km) über dem "
                        f"Radius von {self.options.radius_km:.0f} km",
                        distance,
                    )
            return (None, distance)

        # 'both': keine harte Entfernungsschranke, nur Information.
        return (None, distance)

    def _is_problematic(self, category: str | None, model: str) -> bool:
        if not category:
            return False
        entry = self.knowledge.find_entry(category, model)
        return bool(entry and (entry.problematic or entry.exclude))
