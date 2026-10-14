"""Reproduzierbarer Benchmark der Deal-Bewertung (Abschnitt 17).

Geprüft wird, ob bekannte gute Deals höher bewertet werden als überteuerte
oder riskante Angebote.

Wichtig zur Einordnung: Die hier verwendeten Preise sind BEISPIELDATEN für den
Benchmark. Sie sind keine echten Marktpreise und werden nirgendwo sonst in der
Anwendung als Marktwert verwendet. Die Marktwerte selbst stammen in der
Anwendung aus beobachteten Angeboten und/oder der Wissensbasis des Benutzers.

Das Verhältnis der Scores ist die eigentliche Prüfung, nicht die absoluten
Zahlen.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from ..config import ScoreWeights
from ..knowledge.base import KnowledgeEntry
from ..pricing.market import SOURCE_OBSERVED, MarketEstimate
from ..scoring.deal import (
    VERDICT_GOOD,
    VERDICT_NO_DEAL,
    VERDICT_OUTSTANDING,
    VERDICT_FAIR,
    VERDICT_RISKY,
    ComparableOffer,
    DealScorer,
    ScoringInput,
    verdict_label,
)
from ..scoring.risks import analyze_text_risk

BENCHMARK_MARKET_VALUE = 340.0
BENCHMARK_MODEL = "Beispiel-GPU"

_GOOD_TEXT = (
    "Beispiel-GPU 10GB",
    "Karte funktioniert einwandfrei, mit Rechnung und Originalverpackung. "
    "Wurde nur zum Spielen genutzt und läuft stabil. Abholung oder Versand möglich.",
)
_DEFECT_TEXT = (
    "Beispiel-GPU 10GB defekt",
    "Verkaufe die Karte als Ersatzteil. Bild ist schwarz, vermutlich defekt. "
    "Bastlerartikel, keine Rücknahme, kein Zubehör.",
)


def _market(value: float, *, observations: int = 12, uncertain: bool = False) -> MarketEstimate:
    return MarketEstimate(
        value=value,
        method="observed_median",
        observations=observations,
        used_sources=[SOURCE_OBSERVED],
        median=value,
        q1=round(value * 0.88, 2),
        q3=round(value * 1.1, 2),
        iqr=round(value * 0.22, 2),
        spread_ratio=0.22,
        freshest_at=datetime.now(timezone.utc) - timedelta(days=2),
        uncertainty=0.5 if uncertain else 0.16,
        uncertain=uncertain,
    )


def _knowledge_entry() -> KnowledgeEntry:
    return KnowledgeEntry(
        category="gpu",
        model=BENCHMARK_MODEL,
        manufacturer="Beispielhersteller",
        product_family="Beispielserie",
        good_price_max=round(BENCHMARK_MARKET_VALUE * 0.85, 2),
        fair_price_max=BENCHMARK_MARKET_VALUE,
        overpriced_above=round(BENCHMARK_MARKET_VALUE * 1.15, 2),
        performance_index=100.0,
        source="benchmark_fixture",
    )


def _comparables() -> list[ComparableOffer]:
    return [
        ComparableOffer(
            listing_id=index,
            model=BENCHMARK_MODEL,
            total_price=BENCHMARK_MARKET_VALUE + index * 12.0,
            condition_rank=2,
            performance_index=100.0,
        )
        for index in range(8)
    ]


def _base_input(*, total: float, condition_rank: int, text: tuple[str, str]) -> ScoringInput:
    now = datetime.now(timezone.utc)
    return ScoringInput(
        title=text[0],
        description=text[1],
        category="gpu",
        model=BENCHMARK_MODEL,
        model_confidence=0.9,
        price=total - 5.0,
        shipping_cost=5.0,
        shipping_known=True,
        total_price=total,
        condition_rank=condition_rank,
        published_at=now - timedelta(days=3),
        has_image=True,
        market=_market(BENCHMARK_MARKET_VALUE),
        knowledge_entry=_knowledge_entry(),
        comparables=_comparables(),
        text_risk=analyze_text_risk(*text),
        now=now,
    )


@dataclass
class BenchmarkCase:
    name: str
    description: str
    data: ScoringInput
    expectation: str
    expected_verdicts: tuple[str, ...] = ()
    min_score: float | None = None
    max_score: float | None = None
    extra: dict = field(default_factory=dict)


def benchmark_cases() -> list[BenchmarkCase]:
    now = datetime.now(timezone.utc)

    excellent = _base_input(total=200.0, condition_rank=3, text=_GOOD_TEXT)

    around_market = _base_input(total=338.0, condition_rank=2, text=_GOOD_TEXT)

    overpriced = _base_input(total=470.0, condition_rank=2, text=_GOOD_TEXT)

    risky = _base_input(total=120.0, condition_rank=0, text=_DEFECT_TEXT)

    uncertain_market = _base_input(total=150.0, condition_rank=2, text=_GOOD_TEXT)
    uncertain_market.market = _market(BENCHMARK_MARKET_VALUE, observations=1, uncertain=True)
    uncertain_market.comparables = []

    no_model = ScoringInput(
        title="Grafikkarte",
        description="",
        category="gpu",
        model=None,
        model_confidence=0.0,
        price=90.0,
        total_price=90.0,
        shipping_known=False,
        condition_rank=None,
        published_at=None,
        market=_market(BENCHMARK_MARKET_VALUE),
        comparables=_comparables(),
        text_risk=analyze_text_risk("Grafikkarte", ""),
        now=now,
    )

    gone = _base_input(total=190.0, condition_rank=3, text=_GOOD_TEXT)
    gone.status = "gone"

    return [
        BenchmarkCase(
            name="excellent",
            description=(
                "Sehr günstiges Angebot, guter Zustand, belastbare Marktdaten - "
                "muss ein herausragender Deal sein."
            ),
            data=excellent,
            expectation="Score >= 75 und Urteil 'Herausragender Deal' oder 'Guter Deal'",
            expected_verdicts=(VERDICT_OUTSTANDING, VERDICT_GOOD),
            min_score=75.0,
        ),
        BenchmarkCase(
            name="around_market",
            description="Preis praktisch auf Marktniveau - darf kein herausragender Deal sein.",
            data=around_market,
            expectation="Urteil 'Fairer Preis' oder schlechter, Score < 75",
            expected_verdicts=(VERDICT_FAIR, VERDICT_NO_DEAL),
            max_score=75.0,
        ),
        BenchmarkCase(
            name="overpriced",
            description="Deutlich über Marktwert - muss schlecht bewertet werden.",
            data=overpriced,
            expectation="Score < 55 und Urteil nicht 'Guter Deal'",
            expected_verdicts=(VERDICT_NO_DEAL, VERDICT_FAIR),
            max_score=55.0,
        ),
        BenchmarkCase(
            name="risky_but_cheap",
            description=(
                "Sehr billig, aber als defekt/Ersatzteil gekennzeichnet - trotz "
                "niedrigem Preis als riskant zu bewerten."
            ),
            data=risky,
            expectation="Urteil 'Potenziell riskant'",
            expected_verdicts=(VERDICT_RISKY,),
        ),
        BenchmarkCase(
            name="weak_market_data",
            description=(
                "Nur eine Vergleichsbeobachtung - Marktwert unsicher, "
                "Vertrauen muss sinken."
            ),
            data=uncertain_market,
            expectation="Vertrauen <= 0.75 (Unsicherheit wird ausgewiesen)",
            extra={"max_confidence": 0.75},
        ),
        BenchmarkCase(
            name="no_model",
            description=(
                "Kein Modell erkennbar - darf trotz niedrigem Preis keinen "
                "Top-Score erhalten (Informationsqualität)."
            ),
            data=no_model,
            expectation="Urteil 'Zu wenig Daten' und Score <= 45",
            expected_verdicts=("insufficient_data",),
            max_score=45.0,
        ),
        BenchmarkCase(
            name="not_available",
            description="Nicht mehr verfügbares Angebot - darf nicht als aktiver Deal erscheinen.",
            data=gone,
            expectation="Urteil 'Potenziell riskant' und Score <= 15",
            expected_verdicts=(VERDICT_RISKY,),
            max_score=15.0,
        ),
    ]


def run_benchmark(weights: ScoreWeights | None = None) -> dict:
    """Führt den Benchmark aus. Es wird nichts simuliert - nur echte Score-Aufrufe."""
    scorer = DealScorer(weights)
    cases_out: list[dict] = []
    scores: dict[str, float] = {}
    passed = 0
    failed = 0
    notes: list[str] = []

    for case in benchmark_cases():
        assessment = scorer.score(case.data)
        scores[case.name] = assessment.score
        ok = True
        reasons: list[str] = []
        if case.expected_verdicts and assessment.verdict not in case.expected_verdicts:
            ok = False
            reasons.append(
                f"Urteil war '{verdict_label(assessment.verdict)}', erwartet "
                + " oder ".join(verdict_label(v) for v in case.expected_verdicts)
            )
        if case.min_score is not None and assessment.score < case.min_score:
            ok = False
            reasons.append(f"Score {assessment.score} < {case.min_score}")
        if case.max_score is not None and assessment.score > case.max_score:
            ok = False
            reasons.append(f"Score {assessment.score} > {case.max_score}")
        max_confidence = case.extra.get("max_confidence")
        if max_confidence is not None and assessment.confidence > max_confidence:
            ok = False
            reasons.append(
                f"Vertrauen {assessment.confidence} > {max_confidence} bei schwacher Datenlage"
            )
        if ok:
            passed += 1
        else:
            failed += 1
        cases_out.append(
            {
                "name": case.name,
                "description": case.description,
                "score": assessment.score,
                "verdict": assessment.verdict,
                "verdict_label": assessment.verdict_label,
                "confidence": assessment.confidence,
                "expectation": case.expectation,
                "passed": ok,
                "failures": reasons,
                "price_advantage_pct": assessment.price_advantage_pct,
            }
        )

    # Ordnungspruefung: gute Deals muessen besser sein als schlechte.
    ordering = [
        ("excellent", "overpriced", "Der gute Deal muss besser sein als der überteuerte."),
        ("excellent", "risky_but_cheap", "Der gute Deal muss besser sein als der riskante."),
        ("around_market", "overpriced", "Marktpreis muss besser sein als überteuert."),
        ("excellent", "no_model", "Ein klarer Deal muss besser sein als ein unklares Angebot."),
        ("around_market", "not_available", "Ein verfügbares Angebot muss besser sein als ein vergangenes."),
    ]
    for better, worse, why in ordering:
        if better in scores and worse in scores:
            if scores[better] > scores[worse]:
                passed += 1
            else:
                failed += 1
                notes.append(
                    f"Ordnungsprüfung fehlgeschlagen: {better} ({scores[better]}) war nicht "
                    f"besser als {worse} ({scores[worse]}). {why}"
                )
            cases_out.append(
                {
                    "name": f"ordnung:{better}>{worse}",
                    "description": why,
                    "score": scores[better],
                    "verdict": None,
                    "verdict_label": None,
                    "confidence": None,
                    "expectation": f"{better} > {worse}",
                    "passed": scores[better] > scores[worse],
                    "failures": [] if scores[better] > scores[worse] else [why],
                    "price_advantage_pct": None,
                }
            )

    if not notes:
        notes.append(
            "Alle Bewertungsregeln und Ordnungsprüfungen erfüllt. Die Beispielpreise "
            "sind reine Benchmark-Daten und keine echten Marktpreise."
        )
    return {"cases": cases_out, "passed": passed, "failed": failed, "notes": notes}
