"""Deterministisches Deal-Ranking (Abschnitt 12).

Der Score ist vollständig deterministisch und reproduzierbar: Gleiche
Eingangsdaten und gleiche Gewichte ergeben immer denselben Score. Jede Zahl
ist im Detail nachrechenbar.

Abgebildet werden die Kriterien A-J:

====  ==========================  =========================================
A     Preisvorteil                Gesamtpreis vs. Marktwert
B     Preis-Leistung              Leistungsindex je Euro (nur mit echten Daten)
C     Datenqualität              Belastbarkeit von Modell/Marktwert
D     Zustand und Risiko          Zustandsrang + Textrisiken + Verkäufer
E     Gesamtkosten                Versandkosten relativ zum Artikelpreis
F     Aktualität                 Alter der Anzeige; nicht verfügbar = Abwertung
G     Modellbesonderheiten        Eintrag in der Wissensbasis
H     Standort und Abholung       Entfernung und Fahrtaufwand
I     Marktvergleich              Anzahl und Streuung vergleichbarer Angebote
J     Informationsqualität       Umfang und Klarheit der Angaben
====  ==========================  =========================================
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..config import ScoreWeights
from ..domain import normalize
from ..domain.buying import FormatDecision
from ..knowledge.base import KnowledgeEntry, KnowledgePriceReference
from ..models import STATUS_ACTIVE, STATUS_GONE
from ..pricing.market import CONDITION_FACTORS, MarketEstimate, condition_factor
from .risks import TextRiskFindings

VERDICT_OUTSTANDING = "outstanding"
VERDICT_GOOD = "good"
VERDICT_FAIR = "fair"
VERDICT_NO_DEAL = "no_deal"
VERDICT_RISKY = "risky"
VERDICT_INSUFFICIENT = "insufficient_data"

VERDICT_LABELS: dict[str, str] = {
    VERDICT_OUTSTANDING: "Herausragender Deal",
    VERDICT_GOOD: "Guter Deal",
    VERDICT_FAIR: "Fairer Preis",
    VERDICT_NO_DEAL: "Kein besonderer Deal",
    VERDICT_RISKY: "Potenziell riskant",
    VERDICT_INSUFFICIENT: "Zu wenig Daten",
}


def verdict_label(key: str | None) -> str:
    return VERDICT_LABELS.get(key or "", "Unbekannt")


@dataclass
class ComparableOffer:
    """Ein vergleichbares Angebot für Marktvergleich und Preis-Leistung."""

    listing_id: int | None
    model: str | None
    total_price: float | None
    condition_rank: int | None = None
    performance_index: float | None = None
    title: str | None = None


@dataclass
class ScoringInput:
    """Alle Eingangsdaten der Bewertung - bewusst explizit und vollständig."""

    title: str = ""
    description: str | None = None
    category: str | None = None
    model: str | None = None
    variant: str | None = None
    model_confidence: float = 0.0
    model_ambiguous: bool = False

    price: float | None = None
    shipping_cost: float | None = None
    shipping_known: bool = False
    total_price: float | None = None

    pickup_available: bool = False
    pickup_distance_km: float | None = None
    buyer_postal_code: str | None = None
    seller_postal_code: str | None = None

    condition_rank: int | None = None
    condition_text: str | None = None

    published_at: datetime | None = None
    status: str = STATUS_ACTIVE
    format_decision: FormatDecision | None = None

    seller_feedback_percent: float | None = None
    has_image: bool = False

    market: MarketEstimate | None = None
    knowledge_entry: KnowledgeEntry | None = None
    knowledge_reference: KnowledgePriceReference | None = None
    comparables: list[ComparableOffer] = field(default_factory=list)
    text_risk: TextRiskFindings = field(default_factory=TextRiskFindings)
    now: datetime | None = None

    @property
    def effective_total(self) -> float | None:
        if self.total_price is not None:
            return self.total_price
        return self.price


@dataclass
class ComponentScore:
    key: str
    label: str
    weight: float
    subscore: float
    explanation: str

    @property
    def contribution(self) -> float:
        return self.weight * self.subscore

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "weight": round(self.weight, 2),
            "subscore": round(self.subscore, 3),
            "contribution": round(self.contribution, 3),
            "explanation": self.explanation,
        }


@dataclass
class DealAssessment:
    score: float
    base_score: float
    ceiling: float | None
    confidence: float
    verdict: str
    components: list[ComponentScore]
    reasons: list[str]
    warnings: list[str]
    price_advantage_abs: float | None
    price_advantage_pct: float | None
    market_value: float | None
    risk_score: float
    scored_at: datetime

    @property
    def verdict_label(self) -> str:
        return verdict_label(self.verdict)

    def to_breakdown(self) -> dict:
        return {
            "score": round(self.score, 1),
            "base_score": round(self.base_score, 1),
            "ceiling": None if self.ceiling is None else round(self.ceiling, 1),
            "confidence": round(self.confidence, 3),
            "verdict": self.verdict,
            "verdict_label": self.verdict_label,
            "risk_score": round(self.risk_score, 3),
            "price_advantage_abs": self.price_advantage_abs,
            "price_advantage_pct": self.price_advantage_pct,
            "market_value": self.market_value,
            "components": [c.to_dict() for c in self.components],
            "reasons": list(self.reasons),
            "warnings": list(self.warnings),
        }


def _lerp_curve(value: float, points: tuple[tuple[float, float], ...]) -> float:
    """Stückweise lineare Abbildung mit Sättigung an den Enden."""
    if not points:
        return 0.0
    if value <= points[0][0]:
        return points[0][1]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if value <= x1:
            if x1 == x0:
                return y1
            ratio = (value - x0) / (x1 - x0)
            return y0 + (y1 - y0) * ratio
    return points[-1][1]


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _percentile_rank(values: list[float], target: float) -> float:
    if not values:
        return 0.5
    below = sum(1 for v in values if v < target)
    equal = sum(1 for v in values if v == target)
    return (below + 0.5 * equal) / len(values)


# Zustandsbasis für den Score. Wichtig: Ein normaler Gebrauchtzustand ist
# KEIN Deal-Vorteil und liegt deshalb auf dem neutralen Wert 0.5. Nur 'sehr
# gut' und 'neu' erzeugen echte Pluspunkte, 'Defekt' kostet stark.
_UNKNOWN_CONDITION_BASE = 0.40
_CONDITION_BASE: dict[int, float] = {
    0: 0.00,  # Defekt / Ersatzteil
    1: 0.25,  # Akzeptabel
    2: 0.50,  # Gebraucht, gut (neutral)
    3: 0.80,  # Sehr gut / wie neu
    4: 1.00,  # Neu / ungeöffnet
}


class DealScorer:
    """Berechnet den nachvollziehbaren Deal-Score (0..100)."""

    def __init__(
        self,
        weights: ScoreWeights | None = None,
        *,
        outstanding_threshold: float = 80.0,
        good_threshold: float = 70.0,
        fair_threshold: float = 55.0,
        risky_threshold: float = 0.5,
    ) -> None:
        self.weights = weights or ScoreWeights()
        self.outstanding_threshold = outstanding_threshold
        self.good_threshold = good_threshold
        self.fair_threshold = fair_threshold
        self.risky_threshold = risky_threshold

    # -- Hauptfunktion -----------------------------------------------------

    def score(self, data: ScoringInput) -> DealAssessment:
        now = data.now or datetime.now(timezone.utc)
        market_value = data.market.value if data.market else None
        total = data.effective_total

        advantage_abs: float | None = None
        advantage_pct: float | None = None
        if market_value and total is not None:
            advantage_abs = round(market_value - total, 2)
            advantage_pct = round((market_value - total) / market_value * 100.0, 1)

        components = [
            self._component_price_advantage(data, total, market_value, advantage_pct),
            self._component_price_performance(data, total),
            self._component_data_quality(data),
            self._component_condition_risk(data),
            self._component_total_cost(data, total),
            self._component_freshness(data, now),
            self._component_knowledge(data),
            self._component_location(data),
            self._component_market_comparison(data, total),
            self._component_information_quality(data),
        ]

        total_weight = sum(max(0.0, c.weight) for c in components)
        if total_weight <= 0:  # pragma: no cover - Konfigurationsfehler
            base = 0.0
        else:
            base = 100.0 * sum(c.contribution for c in components) / total_weight

        by_key = {c.key: c for c in components}
        confidence = self._confidence(data, market_value)

        # Decke aus der Datenqualität: Ein niedriger Preis darf keinen
        # Top-Score erzeugen, wenn Modell, Marktwert oder Zustand unklar sind.
        # Kriterium J wird zusätzlich über harte Modell-Gates erzwungen.
        data_quality = by_key["data_quality"].subscore
        ceiling = 100.0 * (0.60 + 0.40 * data_quality)
        if data.model is None:
            ceiling = min(ceiling, 45.0)
        elif data.model_confidence < 0.4:
            ceiling = min(ceiling, 62.0)
        elif data.model_ambiguous:
            ceiling = min(ceiling, 74.0)
        score = min(base, ceiling)

        # Nicht mehr verfügbar: darf nie als aktiver Deal erscheinen.
        if data.status == STATUS_GONE:
            score = min(score, 5.0)
            ceiling = min(ceiling, 5.0)

        verdict = self._verdict_score(
            score=score,
            data=data,
            confidence=confidence,
            advantage_pct=advantage_pct,
            market_value=market_value,
            by_key=by_key,
        )

        reasons = self._reasons(data, by_key, advantage_abs, advantage_pct, market_value)
        warnings = self._warnings(data, market_value, advantage_pct)

        return DealAssessment(
            score=round(score, 1),
            base_score=round(base, 1),
            ceiling=round(ceiling, 1),
            confidence=round(confidence, 3),
            verdict=verdict,
            components=components,
            reasons=reasons,
            warnings=warnings,
            price_advantage_abs=advantage_abs,
            price_advantage_pct=advantage_pct,
            market_value=market_value,
            risk_score=round(data.text_risk.risk_score, 3),
            scored_at=now,
        )

    # -- Komponenten A-J ---------------------------------------------------

    def _component_price_advantage(
        self, data: ScoringInput, total: float | None, market_value: float | None,
        advantage_pct: float | None,
    ) -> ComponentScore:
        weight = self.weights.price_advantage
        if total is None or not market_value:
            note = "Kein Gesamtpreis" if total is None else "Kein Marktwert bestimmbar"
            return ComponentScore("price_advantage", "Preisvorteil", weight, 0.0, note)
        ratio = total / market_value
        sub = _clamp01(
            _lerp_curve(
                ratio,
                ((0.50, 1.00), (0.65, 0.96), (0.80, 0.82), (0.95, 0.55), (1.05, 0.28),
                 (1.20, 0.05), (1.35, 0.0)),
            )
        )
        sign = "unter" if (advantage_pct or 0) >= 0 else "über"
        return ComponentScore(
            "price_advantage",
            "Preisvorteil",
            weight,
            sub,
            f"Gesamtpreis {total:.2f} EUR liegt {abs(advantage_pct or 0):.1f} % {sign} "
            f"dem Marktwert von {market_value:.2f} EUR",
        )

    def _component_price_performance(self, data: ScoringInput, total: float | None) -> ComponentScore:
        weight = self.weights.price_performance
        perf = None
        if data.knowledge_entry is not None:
            perf = data.knowledge_entry.performance_index
        if data.knowledge_reference is not None and perf is None:
            perf = None

        if perf is None or total is None or total <= 0:
            return ComponentScore(
                "price_performance",
                "Preis-Leistung",
                weight,
                0.5,
                "Keine belastbaren Leistungsdaten hinterlegt - neutral bewertet",
            )

        ppe = perf / total
        comparable_ppes = [
            (c.performance_index / c.total_price)
            for c in data.comparables
            if c.performance_index and c.total_price and c.total_price > 0
        ]
        if len(comparable_ppes) < 4:
            return ComponentScore(
                "price_performance",
                "Preis-Leistung",
                weight,
                0.5,
                f"Leistungsindex {perf:.1f} vorhanden, aber nur "
                f"{len(comparable_ppes)} Vergleichsangebote mit Leistungsdaten",
            )
        percentile = _percentile_rank(comparable_ppes, ppe)
        sub = _clamp01(0.10 + 0.90 * percentile)
        return ComponentScore(
            "price_performance",
            "Preis-Leistung",
            weight,
            sub,
            f"Leistung je Euro liegt im {percentile * 100:.0f}. Perzentil von "
            f"{len(comparable_ppes)} vergleichbaren Angeboten",
        )

    def _component_data_quality(self, data: ScoringInput) -> ComponentScore:
        weight = self.weights.data_quality
        parts: list[tuple[str, float]] = []
        parts.append(("Modellzuordnung", _clamp01(data.model_confidence if data.model else 0.0)))
        if data.market is not None:
            parts.append(("Marktwert-Sicherheit", _clamp01(1.0 - data.market.uncertainty)))
            parts.append(("Anzahl Marktbeobachtungen", _clamp01(data.market.observations / 10.0)))
        else:
            parts.append(("Marktwert-Sicherheit", 0.0))
        parts.append(("Versandkosten bekannt", 1.0 if data.shipping_known else 0.65))
        parts.append(("Zustand bekannt", 1.0 if data.condition_rank is not None else 0.55))
        parts.append(("Veröffentlichungsdatum bekannt", 1.0 if data.published_at else 0.6))
        sub = sum(v for _, v in parts) / len(parts)
        detail = ", ".join(f"{name}: {value * 100:.0f} %" for name, value in parts)
        return ComponentScore("data_quality", "Datenqualität", weight, _clamp01(sub), detail)

    def _component_condition_risk(self, data: ScoringInput) -> ComponentScore:
        weight = self.weights.condition_risk
        base = (
            _CONDITION_BASE.get(data.condition_rank, _UNKNOWN_CONDITION_BASE)
            if data.condition_rank is not None
            else _UNKNOWN_CONDITION_BASE
        )
        sub = base
        details = [
            f"Zustand: {normalize.CONDITION_LABELS.get(data.condition_rank if data.condition_rank is not None else normalize.CONDITION_UNKNOWN, 'Unbekannt')}"
        ]

        risk = data.text_risk.risk_score
        if risk > 0:
            sub -= 0.6 * risk
            details.append(f"Textrisiken: {risk * 100:.0f} %")
        positive = data.text_risk.positive_score
        if positive > 0:
            sub += 0.15 * positive
            details.append(f"Positive Signale: {positive * 100:.0f} %")

        if data.seller_feedback_percent is not None:
            if data.seller_feedback_percent < 90:
                sub -= 0.12
                details.append(f"Verkäuferbewertung {data.seller_feedback_percent:.0f} % (niedrig)")
            elif data.seller_feedback_percent >= 99:
                sub += 0.05
                details.append("Verkäuferbewertung sehr hoch")

        if data.condition_rank is None:
            details.append("Zustand nicht angegeben")
        return ComponentScore("condition_risk", "Zustand und Risiko", weight, _clamp01(sub),
                              "; ".join(details))

    def _component_total_cost(self, data: ScoringInput, total: float | None) -> ComponentScore:
        weight = self.weights.total_cost
        if total is None:
            return ComponentScore("total_cost", "Gesamtkosten", weight, 0.0,
                                  "Kein Gesamtpreis bestimmbar")
        if not data.shipping_known:
            if data.pickup_available:
                return ComponentScore(
                    "total_cost", "Gesamtkosten", weight, 0.60,
                    "Versandkosten unbekannt, Abholung möglich",
                )
            return ComponentScore(
                "total_cost", "Gesamtkosten", weight, 0.45,
                "Versandkosten unbekannt - Gesamtkosten unsicher",
            )
        shipping = data.shipping_cost or 0.0
        price = data.price if data.price and data.price > 0 else total
        if shipping <= 0:
            return ComponentScore("total_cost", "Gesamtkosten", weight, 1.0,
                                  "Keine Versandkosten")
        ratio = shipping / price if price else 1.0
        sub = _clamp01(
            _lerp_curve(
                ratio,
                ((0.0, 1.00), (0.03, 0.95), (0.08, 0.85), (0.15, 0.65), (0.30, 0.35),
                 (0.50, 0.15), (1.00, 0.05)),
            )
        )
        return ComponentScore("total_cost", "Gesamtkosten", weight, sub,
                              f"Versand {shipping:.2f} EUR = {ratio * 100:.1f} % des Artikelpreises")

    def _component_freshness(self, data: ScoringInput, now: datetime) -> ComponentScore:
        weight = self.weights.freshness
        if data.status == STATUS_GONE:
            return ComponentScore("freshness", "Aktualität", weight, 0.0,
                                  "Angebot ist nicht mehr verfügbar")
        if data.published_at is None:
            return ComponentScore(
                "freshness", "Aktualität", weight, 0.35,
                "Veröffentlichungszeitpunkt unbekannt - Alter nicht verifizierbar",
            )
        days = normalize.age_days(data.published_at, now)
        if days is None:  # pragma: no cover - defensiv
            return ComponentScore("freshness", "Aktualität", weight, 0.35,
                                  "Alter nicht berechenbar")
        sub = _clamp01(
            _lerp_curve(
                days,
                ((0, 1.00), (3, 0.95), (7, 0.85), (30, 0.62), (90, 0.42), (150, 0.32),
                 (300, 0.12), (700, 0.02)),
            )
        )
        return ComponentScore("freshness", "Aktualität", weight, sub,
                              f"Angebot ist {days:.0f} Tage alt")

    def _component_knowledge(self, data: ScoringInput) -> ComponentScore:
        weight = self.weights.knowledge_match
        entry = data.knowledge_entry
        reference = data.knowledge_reference
        if entry is None and reference is None:
            return ComponentScore(
                "knowledge_match", "Wissensbasis", weight, 0.35,
                "Kein Eintrag in der Wissensbasis - Bewertung stützt sich nur auf Marktdaten",
            )
        if entry is not None and (entry.problematic or entry.exclude):
            return ComponentScore(
                "knowledge_match", "Wissensbasis", weight, 0.10,
                "Modell ist in der Wissensbasis als problematisch markiert",
            )
        has_prices = reference is not None and reference.target_value() is not None
        sub = 0.85 if has_prices else 0.50
        details: list[str] = []
        if has_prices:
            details.append("Manuelles Preiswissen vorhanden")
        else:
            details.append("Nur Identitätsdaten, keine Preisrichtwerte")
        if entry is not None and entry.weight and entry.weight > 1.0:
            sub = min(1.0, sub + 0.1)
            details.append("Vom Benutzer bevorzugtes Modell")
        issues = list((entry.known_issues if entry else []) or (reference.known_issues if reference else []))
        if issues:
            sub = max(0.1, sub - 0.15)
            details.append("Bekannte Modellprobleme: " + "; ".join(issues[:2]))
        return ComponentScore("knowledge_match", "Wissensbasis", weight, _clamp01(sub),
                              " - ".join(details))

    def _component_location(self, data: ScoringInput) -> ComponentScore:
        weight = self.weights.location
        if not data.pickup_available:
            return ComponentScore(
                "location", "Standort/Abholung", weight, 0.50,
                "Versandangebot - Standort spielt keine Rolle (neutral)",
            )
        if data.pickup_distance_km is None:
            reason = (
                "Abholung erforderlich, Entfernung unbekannt (keine belastbaren PLZ-Daten)"
            )
            return ComponentScore("location", "Standort/Abholung", weight, 0.35, reason)
        distance = data.pickup_distance_km
        sub = _clamp01(
            _lerp_curve(
                distance,
                ((0, 1.00), (15, 0.92), (40, 0.78), (80, 0.58), (150, 0.35),
                 (300, 0.12), (600, 0.02)),
            )
        )
        label = f"Abholung ca. {distance:.0f} km entfernt"
        if distance > 0:
            label += " (Entfernung grob geschätzt aus PLZ-Zone)"
        return ComponentScore("location", "Standort/Abholung", weight, sub, label)

    def _component_market_comparison(
        self, data: ScoringInput, total: float | None
    ) -> ComponentScore:
        weight = self.weights.market_comparison
        if data.market is None or data.market.value is None:
            return ComponentScore("market_comparison", "Marktvergleich", weight, 0.15,
                                  "Keine Marktpreisbasis vorhanden")
        n = data.market.observations
        sub = _lerp_curve(n, ((0, 0.10), (2, 0.28), (4, 0.42), (8, 0.60), (15, 0.80), (30, 1.0)))
        details = [f"{n} vergleichbare Angebote"]
        if data.market.spread_ratio is not None:
            details.append(f"Streuung (IQR/Median) {data.market.spread_ratio:.2f}")
            if data.market.spread_ratio > 0.6:
                sub -= 0.15
                details.append("hohe Streuung")
        if data.market.q1 is not None and total is not None and total < data.market.q1:
            sub += 0.15
            details.append("Preis unter dem unteren Quartil")
        if data.market.freshest_at is not None:
            age = normalize.age_days(data.market.freshest_at, data.now)
            if age is not None and age > 45:
                sub -= 0.1
                details.append("Vergleichsdaten älter als 45 Tage")
        if data.market.uncertain:
            sub -= 0.1
            details.append("Marktwert unsicher")
        return ComponentScore("market_comparison", "Marktvergleich", weight, _clamp01(sub),
                              "; ".join(details))

    def _component_information_quality(self, data: ScoringInput) -> ComponentScore:
        """Kriterium J: Umfang und Klarheit der Angaben.

        Die Modellklarheit dominiert bewusst - sie ist die wichtigste
        Information für die Vergleichbarkeit. Eine kurze, aber präzise
        Beschreibung soll nicht schlechter bewertet werden als eine lange,
        unklare.
        """
        weight = self.weights.information_quality
        description = data.description or ""
        details: list[str] = [f"Beschreibung {len(description)} Zeichen"]

        if data.model:
            sub = 0.35 + 0.15 * _clamp01(data.model_confidence)
            details.append(f"Modell erkannt: {data.model}")
        else:
            sub = 0.05
            details.append("Kein Modell erkennbar")

        sub += _lerp_curve(
            len(description), ((0, 0.0), (50, 0.10), (150, 0.20), (400, 0.30), (900, 0.35))
        )
        if data.variant:
            sub += 0.10
            details.append(f"Variante: {data.variant}")
        if data.condition_rank is not None:
            sub += 0.08
        if data.has_image:
            sub += 0.07
            details.append("Produktbild vorhanden")
        if data.shipping_known:
            sub += 0.05
        if data.model_ambiguous:
            sub -= 0.10
            details.append("Modellzuordnung mehrdeutig")
        if not data.model:
            sub = min(sub, 0.20)
        return ComponentScore("information_quality", "Informationsqualität", weight,
                              _clamp01(sub), "; ".join(details))

    # -- Gesamturteil ------------------------------------------------------

    def _confidence(self, data: ScoringInput, market_value: float | None) -> float:
        model_term = _clamp01(data.model_confidence) if data.model else 0.0
        if data.market is not None:
            # Die Marktsicherheit wird zusätzlich nach der Datenmenge
            # gestaffelt: Eine einzelne Vergleichsbeobachtung traegt deutlich
            # weniger als ein breiter Vergleich.
            support = 0.4 + 0.6 * min(1.0, data.market.observations / 6.0)
            market_term = _clamp01(1.0 - data.market.uncertainty) * support
        else:
            market_term = 0.0
        completeness = 0.0
        completeness += 0.34 if data.shipping_known else 0.0
        completeness += 0.33 if data.condition_rank is not None else 0.0
        completeness += 0.33 if data.published_at is not None else 0.0
        confidence = 0.40 * model_term + 0.40 * market_term + 0.20 * completeness
        if market_value is None:
            confidence = min(confidence, 0.30)
        return _clamp01(confidence)

    def _verdict_score(
        self,
        *,
        score: float,
        data: ScoringInput,
        confidence: float,
        advantage_pct: float | None,
        market_value: float | None,
        by_key: dict[str, ComponentScore],
    ) -> str:
        if market_value is None or not data.model or data.effective_total is None:
            return VERDICT_INSUFFICIENT
        if data.status == STATUS_GONE:
            return VERDICT_RISKY
        if data.text_risk.risk_score >= self.risky_threshold:
            return VERDICT_RISKY

        condition_ok = by_key["condition_risk"].subscore
        if (
            score >= self.outstanding_threshold
            and confidence >= 0.55
            and (advantage_pct or 0.0) >= 25.0
            and condition_ok >= 0.5
        ):
            return VERDICT_OUTSTANDING
        if score >= self.good_threshold:
            return VERDICT_GOOD
        if score >= self.fair_threshold:
            return VERDICT_FAIR
        return VERDICT_NO_DEAL

    def _reasons(
        self,
        data: ScoringInput,
        by_key: dict[str, ComponentScore],
        advantage_abs: float | None,
        advantage_pct: float | None,
        market_value: float | None,
    ) -> list[str]:
        reasons: list[str] = []
        total = data.effective_total
        if market_value and total is not None and advantage_abs is not None:
            if advantage_abs > 0:
                reasons.append(
                    f"{advantage_abs:.2f} EUR ({advantage_pct:.0f} %) unter dem geschätzten "
                    f"Marktwert von {market_value:.2f} EUR"
                )
            else:
                reasons.append(
                    f"{abs(advantage_abs):.2f} EUR über dem geschätzten Marktwert von "
                    f"{market_value:.2f} EUR"
                )
        if data.model:
            label = data.model + (f" ({data.variant})" if data.variant else "")
            reasons.append(f"Erkanntes Modell: {label}")
        if market_value is None:
            reasons.append("Kein belastbarer Marktwert - Bewertung eingeschränkt")
        top = sorted(by_key.values(), key=lambda c: c.contribution, reverse=True)[:3]
        for component in top:
            reasons.append(f"{component.label}: {component.explanation}")
        if data.text_risk.positives:
            reasons.append(
                "Positive Signale: " + ", ".join(p.label for p in data.text_risk.positives[:3])
            )
        return reasons[:7]

    def _warnings(
        self,
        data: ScoringInput,
        market_value: float | None,
        advantage_pct: float | None,
    ) -> list[str]:
        warnings: list[str] = []
        if data.market is not None and data.market.uncertain:
            warnings.append("Marktwert unsicher - Datenlage schwach")
        if market_value is None:
            warnings.append("Kein Marktwert bestimmbar")
        if not data.shipping_known and not data.pickup_available:
            warnings.append("Versandkosten unbekannt")
        if data.published_at is None:
            warnings.append("Veröffentlichungszeitpunkt unbekannt")
        if data.condition_rank is None:
            warnings.append("Zustand nicht angegeben")
        if data.model is None:
            warnings.append("Kein Hardwaremodell sicher erkannt")
        elif data.model_ambiguous:
            warnings.append("Modellzuordnung mehrdeutig")
        for signal in data.text_risk.negatives:
            warnings.append(f"{signal.label} (Schwere {signal.severity * 100:.0f} %)")
        if data.format_decision is not None and data.format_decision.mixed:
            warnings.append("Auktion mit Sofort-Kauf - Festpreis separat angegeben")
        if advantage_pct is not None and advantage_pct < -15:
            warnings.append(f"Preis liegt {abs(advantage_pct):.0f} % über dem Marktwert")
        if data.status != STATUS_ACTIVE:
            warnings.append("Angebotsstatus nicht als aktiv bestätigt")
        return warnings[:9]


# Wiederverwendbare Faktoren für Aufrufer, die Zustandswerte erklären wollen.
__all__ = [
    "ComparableOffer",
    "ComponentScore",
    "DealAssessment",
    "DealScorer",
    "ScoringInput",
    "VERDICT_FAIR",
    "VERDICT_GOOD",
    "VERDICT_INSUFFICIENT",
    "VERDICT_LABELS",
    "VERDICT_NO_DEAL",
    "VERDICT_OUTSTANDING",
    "VERDICT_RISKY",
    "CONDITION_FACTORS",
    "condition_factor",
    "verdict_label",
]
