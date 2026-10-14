"""Marktpreisermittlung (Abschnitt 10).

Strikte Trennung der Quellen:

* ``observed_offers``       - aktuell beobachtete Angebotspreise (aus der
                              lokalen Datenbank, gefiltert auf vergleichbare
                              Modelle und Angebotsformat).
* ``manual_knowledge_base`` - vom Benutzer gepflegte Richtwerte.
* ``algorithmic_estimate``  - nur wenn keine belastbare Datenlage existiert.

Es werden ausdrücklich KEINE tatsächlich erzielten Verkaufspreise
behauptet: Für diese Anwendung steht keine zulässige Verkaufsdatenquelle
zur Verfügung. Sobald eine solche Quelle angebunden wird, muss sie als
eigene Quelle ergänzt werden - die Struktur ist dafür vorbereitet
(``PriceObservation.source``).

Vorgehen: Median und Quartile statt Mittelwert, Ausreißerfilterung über den
Interquartilsabstand, Zustandsanpassung, und eine explizit ausgewiesene
Unsicherheit. Bei schwacher Datenlage lautet das Ergebnis "unsicher".
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..domain.normalize import compact_key
from ..knowledge.base import KnowledgeBase, KnowledgePriceReference
from ..models import FORMAT_FIXED_PRICE, STATUS_ACTIVE, Listing

SOURCE_OBSERVED = "observed_offers"
SOURCE_KNOWLEDGE = "manual_knowledge_base"
SOURCE_ALGORITHMIC = "algorithmic_estimate"

REFERENCE_CONDITION_RANK = 2  # "Gebraucht, gut" als Bezugszustand

# Ab so vielen vergleichbaren Beobachtungen wird überhaupt ein Wert
# ausgewiesen - dann aber ausdrücklich als vorläufig und unsicher.
MIN_OBSERVATIONS_FOR_ANY_ESTIMATE = 3

# Zustandsfaktoren relativ zum Bezugszustand. Heuristisch und bewusst
# konservativ; ohne Zustandsangabe wird nicht angepasst.
CONDITION_FACTORS: dict[int, float] = {
    0: 0.35,  # Defekt / Ersatzteil
    1: 0.78,  # Akzeptabel
    2: 1.00,  # Gebraucht, gut  (Bezug)
    3: 1.12,  # Sehr gut / wie neu
    4: 1.28,  # Neu / ungeöffnet
}


def condition_factor(rank: int | None) -> float:
    if rank is None:
        return 1.0
    return CONDITION_FACTORS.get(rank, 1.0)


@dataclass
class PriceObservation:
    """Eine einzelne Preisbeobachtung."""

    total_price: float
    condition_rank: int | None
    observed_at: datetime
    source: str
    listing_id: int | None = None
    label: str | None = None


@dataclass
class MarketEstimate:
    value: float | None
    method: str
    observations: int
    used_sources: list[str] = field(default_factory=list)
    median: float | None = None
    q1: float | None = None
    q3: float | None = None
    iqr: float | None = None
    spread_ratio: float | None = None
    freshest_at: datetime | None = None
    uncertainty: float = 1.0
    uncertain: bool = True
    knowledge_reference: KnowledgePriceReference | None = None
    condition_adjusted: bool = False
    outliers_removed: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def good_price_threshold(self) -> float | None:
        """Preis, unterhalb dessen ein Angebot als günstig gilt."""
        if self.q1 is not None:
            return round(self.q1, 2)
        if self.knowledge_reference and self.knowledge_reference.good_price_max is not None:
            return round(self.knowledge_reference.good_price_max, 2)
        if self.value is not None:
            return round(self.value * 0.85, 2)
        return None

    def to_basis(self) -> dict:
        return {
            "method": self.method,
            "observations": self.observations,
            "used_sources": list(self.used_sources),
            "median": self.median,
            "q1": self.q1,
            "q3": self.q3,
            "iqr": self.iqr,
            "spread_ratio": self.spread_ratio,
            "freshest_at": self.freshest_at.isoformat() if self.freshest_at else None,
            "uncertainty": round(self.uncertainty, 3),
            "uncertain": self.uncertain,
            "condition_adjusted": self.condition_adjusted,
            "outliers_removed": self.outliers_removed,
            "knowledge_reference": (
                {
                    "model": self.knowledge_reference.model,
                    "source": self.knowledge_reference.source,
                    "version": self.knowledge_reference.version,
                    "good_price_max": self.knowledge_reference.good_price_max,
                    "fair_price_max": self.knowledge_reference.fair_price_max,
                    "overpriced_above": self.knowledge_reference.overpriced_above,
                    "notes": self.knowledge_reference.notes,
                    "known_issues": self.knowledge_reference.known_issues,
                }
                if self.knowledge_reference
                else None
            ),
            "notes": list(self.notes),
            "disclaimer": (
                "Beobachtete Angebotspreise sind keine erzielten Verkaufspreise."
            ),
        }


def _quantile(values: list[float], q: float) -> float:
    """Lineare Quantilsinterpolation (0 <= q <= 1)."""
    if not values:
        raise ValueError("leere Werteliste")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = q * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def remove_outliers(values: list[float]) -> tuple[list[float], int]:
    """Entfernt Ausreißer über den 1.5*IQR-Zaun.

    Erst ab 5 Werten wird gefiltert: Bei sehr kleinen Stichproben ist der
    Interquartilsabstand selbst zu unsicher, um Ausreißer zuverlässig zu
    erkennen - dann würde die Filterung mehr Schäden als Nutzen anrichten.
    """
    if len(values) < 5:
        return list(values), 0
    q1 = _quantile(values, 0.25)
    q3 = _quantile(values, 0.75)
    iqr = q3 - q1
    if iqr <= 0:
        return list(values), 0
    low = q1 - 1.5 * iqr
    high = q3 + 1.5 * iqr
    kept = [v for v in values if low <= v <= high]
    if not kept:  # Extremfall: nie alles verwerfen
        return list(values), 0
    return kept, len(values) - len(kept)


class MarketPriceEstimator:
    """Berechnet belastbare Preisreferenzen für ein Hardwaremodell."""

    def __init__(
        self,
        knowledge: KnowledgeBase,
        *,
        min_observations: int = 5,
        window_days: int = 150,
    ) -> None:
        self.knowledge = knowledge
        self.min_observations = max(3, min_observations)
        self.window_days = window_days

    # -- Oeffentliche API --------------------------------------------------

    def estimate(
        self,
        session: Session | None,
        *,
        category: str | None,
        model: str | None,
        condition_rank: int | None = None,
        exclude_listing_id: int | None = None,
        now: datetime | None = None,
    ) -> MarketEstimate:
        reference = None
        if category and model:
            reference = self.knowledge.find_price_reference(category, model)

        observations = self._collect_observations(
            session,
            category=category,
            model=model,
            exclude_listing_id=exclude_listing_id,
            now=now,
        )
        return self._combine(observations, reference, condition_rank)

    # -- Datenerhebung -----------------------------------------------------

    def _collect_observations(
        self,
        session: Session | None,
        *,
        category: str | None,
        model: str | None,
        exclude_listing_id: int | None,
        now: datetime | None,
    ) -> list[PriceObservation]:
        if session is None or not category or not model:
            return []
        reference_time = now or datetime.now(timezone.utc)
        cutoff = reference_time - timedelta(days=self.window_days)

        stmt = select(Listing).where(
            Listing.category == category,
            Listing.status != "gone",
            Listing.listing_format == FORMAT_FIXED_PRICE,
            Listing.total_price.is_not(None),
        )
        rows = session.scalars(stmt).all()

        target_key = compact_key(model)
        observations: list[PriceObservation] = []
        for row in rows:
            if exclude_listing_id is not None and row.id == exclude_listing_id:
                continue
            if compact_key(row.model) != target_key:
                continue
            observed_at = row.last_seen_at or row.fetched_at
            if observed_at is not None:
                if observed_at.tzinfo is None:
                    observed_at = observed_at.replace(tzinfo=timezone.utc)
                if observed_at < cutoff:
                    continue
            observations.append(
                PriceObservation(
                    total_price=float(row.total_price),
                    condition_rank=row.condition_rank,
                    observed_at=observed_at or reference_time,
                    source=SOURCE_OBSERVED,
                    listing_id=row.id,
                    label=row.title[:80],
                )
            )
        return observations

    # -- Kombination -------------------------------------------------------

    def _combine(
        self,
        observations: list[PriceObservation],
        reference: KnowledgePriceReference | None,
        condition_rank: int | None,
    ) -> MarketEstimate:
        notes: list[str] = []
        target_factor = condition_factor(condition_rank)

        # Zustandsanpassung: Beobachtungen auf den Bezugszustand normieren.
        adjusted: list[float] = []
        adjusted_any = False
        for obs in observations:
            factor = condition_factor(obs.condition_rank)
            if factor and factor > 0:
                adjusted.append(obs.total_price / factor)
                if obs.condition_rank is not None and obs.condition_rank != REFERENCE_CONDITION_RANK:
                    adjusted_any = True
            else:  # pragma: no cover - defensiv
                adjusted.append(obs.total_price)

        filtered, removed = remove_outliers(adjusted)

        observed_value: float | None = None
        median = q1 = q3 = iqr = spread = None
        freshest = None
        provisional = False
        if len(filtered) >= self.min_observations:
            median = round(statistics.median(filtered), 2)
            q1 = round(_quantile(filtered, 0.25), 2)
            q3 = round(_quantile(filtered, 0.75), 2)
            iqr = round(q3 - q1, 2)
            spread = round(iqr / median, 3) if median else None
            freshest = max(o.observed_at for o in observations)
            observed_value = round(median * target_factor, 2)
        elif len(filtered) >= MIN_OBSERVATIONS_FOR_ANY_ESTIMATE:
            # Vorläufige Schätzung: lieber ein klar als unsicher
            # gekennzeichneter Wert als gar keine Aussage.
            provisional = True
            median = round(statistics.median(filtered), 2)
            q1 = round(_quantile(filtered, 0.25), 2)
            q3 = round(_quantile(filtered, 0.75), 2)
            iqr = round(q3 - q1, 2)
            spread = round(iqr / median, 3) if median else None
            freshest = max(o.observed_at for o in observations)
            observed_value = round(median * target_factor, 2)
            notes.append(
                f"Vorläufiger Marktwert aus nur {len(filtered)} vergleichbaren "
                f"Beobachtungen (belastbar ab {self.min_observations})"
            )
        elif observations:
            notes.append(
                f"Nur {len(observations)} vergleichbare Beobachtung(en) - zu wenig "
                f"für eine Marktwertschätzung (mindestens "
                f"{MIN_OBSERVATIONS_FOR_ANY_ESTIMATE} erforderlich)"
            )

        knowledge_value = None
        if reference is not None:
            base = reference.target_value()
            if base is not None:
                knowledge_value = round(base * target_factor, 2)

        used_sources: list[str] = []
        if observed_value is not None:
            used_sources.append(SOURCE_OBSERVED)
        if knowledge_value is not None:
            used_sources.append(SOURCE_KNOWLEDGE)

        value: float | None = None
        method: str
        if provisional:
            method_suffix = "_provisional"
        else:
            method_suffix = ""
        if observed_value is not None and knowledge_value is not None:
            # Median der Beobachtungen ist die objektivere Basis; der manuell
            # gepflegte Wert wirkt als Korrektiv, gewichtet nach seinem
            # Gewicht in der Wissensbasis.
            weight = max(0.0, min(1.0, reference.weight if reference else 0.5))
            blend = 0.25 + 0.5 * weight  # 25 % .. 75 % Gewicht auf Wissensbasis
            value = round(observed_value * (1 - blend) + knowledge_value * blend, 2)
            method = "combined_median_and_knowledge" + method_suffix
            notes.append(
                f"Kombination: beobachteter Median {observed_value:.2f} EUR, "
                f"Wissensbasis {knowledge_value:.2f} EUR (Gewicht {blend:.2f})"
            )
        elif observed_value is not None:
            value = observed_value
            method = "observed_median" + method_suffix
        elif knowledge_value is not None:
            value = knowledge_value
            method = "knowledge_base_reference"
            notes.append(
                "Keine ausreichenden vergleichbaren Angebote - Wert stammt aus "
                "manuell gepflegtem Preiswissen"
            )
        else:
            value = None
            method = SOURCE_ALGORITHMIC
            notes.append("Keine Marktdaten und kein Wissensbasis-Richtwert vorhanden")

        uncertainty = self._uncertainty(
            observations=len(observations),
            filtered=len(filtered),
            spread=spread,
            observed_value=observed_value,
            knowledge_value=knowledge_value,
            reference=reference,
        )
        if provisional:
            # Eine vorläufige Schätzung darf nie als belastbar erscheinen.
            uncertainty = max(uncertainty, 0.6)

        return MarketEstimate(
            value=value,
            method=method,
            observations=len(observations),
            used_sources=used_sources,
            median=median,
            q1=q1,
            q3=q3,
            iqr=iqr,
            spread_ratio=spread,
            freshest_at=freshest,
            uncertainty=uncertainty,
            uncertain=uncertainty > 0.45,
            knowledge_reference=reference,
            condition_adjusted=adjusted_any,
            outliers_removed=removed,
            notes=notes,
        )

    def _uncertainty(
        self,
        *,
        observations: int,
        filtered: int,
        spread: float | None,
        observed_value: float | None,
        knowledge_value: float | None,
        reference: KnowledgePriceReference | None,
    ) -> float:
        """Unsicherheit 0..1. Höher = unsicherer."""
        if observed_value is None and knowledge_value is None:
            return 1.0

        if observed_value is not None:
            # Stichprobengroesse: 5 -> 0.45, 20+ -> ~0.1
            confidence = min(1.0, (filtered - 2) / 18.0)
            score = 0.55 - 0.45 * confidence
            if spread is not None:
                score += min(0.25, spread * 0.5)
            if knowledge_value is not None:
                score -= 0.1  # zweite Quelle stützt die Schätzung
            return round(max(0.05, min(0.95, score)), 3)

        # Nur Wissensbasis
        score = 0.5
        if reference and reference.weight >= 1.0:
            score -= 0.1
        if observations:
            score -= 0.05
        return round(max(0.15, min(0.95, score)), 3)


def describe_uncertainty(estimate: MarketEstimate) -> str:
    if estimate.value is None:
        return "Kein Marktwert bestimmbar"
    if estimate.uncertain:
        return "Marktwert unsicher"
    if estimate.uncertainty > 0.25:
        return "Marktwert mäßig belastbar"
    return "Marktwert belastbar"
