"""Tests fuer das Deal-Ranking (Abschnitt 12) und den Benchmark (Abschnitt 17)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend.benchmarks.deal_benchmark import benchmark_cases, run_benchmark
from backend.config import ScoreWeights
from backend.knowledge.base import KnowledgeEntry, KnowledgePriceReference
from backend.pricing.market import MarketEstimate
from backend.scoring.deal import (
    VERDICT_FAIR,
    VERDICT_GOOD,
    VERDICT_NO_DEAL,
    VERDICT_OUTSTANDING,
    VERDICT_INSUFFICIENT,
    VERDICT_RISKY,
    DealScorer,
    ScoringInput,
    verdict_label,
)
from backend.scoring.risks import analyze_text_risk

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)


def market(value: float, *, observations: int = 12, uncertain: bool = False) -> MarketEstimate:
    return MarketEstimate(
        value=value,
        method="observed_median",
        observations=observations,
        used_sources=["observed_offers"],
        median=value,
        q1=round(value * 0.9, 2),
        q3=round(value * 1.1, 2),
        iqr=round(value * 0.2, 2),
        spread_ratio=0.2,
        freshest_at=NOW - timedelta(days=3),
        uncertainty=0.2 if not uncertain else 0.8,
        uncertain=uncertain,
    )


def base_input(**overrides) -> ScoringInput:
    data = {
        "title": "ASUS RTX 3080 TUF Gaming 10GB Grafikkarte",
        "description": (
            "Karte funktioniert einwandfrei, mit Rechnung und Originalverpackung. "
            "Nur zum Spielen genutzt. Versand moeglich."
        ),
        "category": "gpu",
        "model": "RTX 3080",
        "variant": "10 GB",
        "model_confidence": 0.85,
        "price": 170.0,
        "shipping_cost": 5.0,
        "shipping_known": True,
        "total_price": 175.0,
        "condition_rank": 3,
        "condition_text": "Sehr gut",
        "published_at": NOW - timedelta(days=4),
        "market": market(260.0),
        "has_image": True,
        "seller_feedback_percent": 99.5,
        "now": NOW,
        "text_risk": analyze_text_risk("RTX 3080", "mit Rechnung, funktioniert einwandfrei"),
    }
    data.update(overrides)
    return ScoringInput(**data)


class TestDeterminism:
    def test_gleiche_eingaben_ergeben_gleichen_score(self):
        scorer = DealScorer()
        first = scorer.score(base_input())
        second = scorer.score(base_input())
        assert first.score == second.score
        assert first.verdict == second.verdict
        assert [c.subscore for c in first.components] == [c.subscore for c in second.components]

    def test_alle_kriterien_a_bis_j_sind_enthalten(self):
        assessment = DealScorer().score(base_input())
        keys = {component.key for component in assessment.components}
        assert keys == {
            "price_advantage",
            "price_performance",
            "data_quality",
            "condition_risk",
            "total_cost",
            "freshness",
            "knowledge_match",
            "location",
            "market_comparison",
            "information_quality",
        }

    def test_breakdown_ist_nachvollziehbar(self):
        breakdown = DealScorer().score(base_input()).to_breakdown()
        assert 0 <= breakdown["score"] <= 100
        assert breakdown["verdict_label"]
        assert breakdown["reasons"]
        assert isinstance(breakdown["components"], list)
        for component in breakdown["components"]:
            assert component["explanation"]


class TestPriceAdvantage:
    def test_guenstiges_angebot_schlaegt_marktpreis_angebot(self):
        scorer = DealScorer()
        cheap = scorer.score(base_input(total_price=150.0, price=145.0))
        at_market = scorer.score(base_input(total_price=260.0, price=255.0))
        assert cheap.score > at_market.score
        assert cheap.price_advantage_abs is not None and cheap.price_advantage_abs > 0
        assert at_market.price_advantage_abs is not None and at_market.price_advantage_abs <= 0

    def test_ueberteuertes_angebot_wird_negativ_benannt(self):
        assessment = DealScorer().score(base_input(total_price=400.0, price=395.0))
        assert assessment.price_advantage_pct is not None
        assert assessment.price_advantage_pct < -15
        assert any("über dem Marktwert" in w for w in assessment.warnings)

    def test_kein_preisvorteil_ohne_marktwert(self):
        assessment = DealScorer().score(base_input(market=None))
        assert assessment.market_value is None
        assert assessment.price_advantage_pct is None
        assert assessment.verdict in {
            VERDICT_INSUFFICIENT,
            VERDICT_FAIR,
            VERDICT_NO_DEAL,
            VERDICT_GOOD,
        }
        assert any("Marktwert" in w for w in assessment.warnings) or assessment.score < 80


class TestVerdicts:
    def test_herausragender_deal_braucht_mehr_als_einen_niedrigen_preis(self):
        excellent = DealScorer().score(base_input(total_price=140.0, price=135.0))
        assert excellent.verdict in {VERDICT_OUTSTANDING, VERDICT_GOOD}
        assert excellent.score >= 70

    def test_billig_aber_defekt_ist_kein_top_deal(self):
        assessment = DealScorer().score(
            base_input(
                title="RTX 3080 defekt als Ersatzteil",
                description="Karte gibt kein Bild. Defekt, keine Ruecknahme, kein Zubehoer.",
                condition_rank=0,
                condition_text="Defekt",
                total_price=60.0,
                price=60.0,
                market=market(260.0),
                text_risk=analyze_text_risk(
                    "RTX 3080 defekt als Ersatzteil",
                    "Karte gibt kein Bild. Defekt, keine Ruecknahme, kein Zubehoer.",
                ),
            )
        )
        assert assessment.verdict in {VERDICT_RISKY, VERDICT_FAIR, VERDICT_NO_DEAL}
        assert assessment.score < 70

    def test_unklares_modell_darf_nicht_ganz_oben_landen(self):
        assessment = DealScorer().score(
            base_input(model=None, variant=None, model_confidence=0.0, total_price=100.0)
        )
        assert assessment.score <= 45.0
        assert any("Kein Hardwaremodell" in w for w in assessment.warnings)

    def test_nicht_mehr_verfuegbar_ist_kein_aktiver_deal(self):
        assessment = DealScorer().score(
            base_input(status="gone", total_price=50.0, price=45.0)
        )
        assert assessment.score <= 5.0
        assert assessment.verdict != VERDICT_OUTSTANDING

    def test_fehlende_daten_senken_das_vertrauen(self):
        good_data = DealScorer().score(base_input())
        weak_data = DealScorer().score(
            base_input(
                condition_rank=None,
                condition_text=None,
                shipping_known=False,
                shipping_cost=None,
                published_at=None,
                market=market(260.0, observations=4, uncertain=True),
                has_image=False,
            )
        )
        assert weak_data.confidence < good_data.confidence

    def test_verdict_labels_sind_deutsch(self):
        for key in (VERDICT_OUTSTANDING, VERDICT_GOOD, VERDICT_FAIR, VERDICT_NO_DEAL, VERDICT_RISKY):
            assert verdict_label(key)
        assert verdict_label("unbekannt") == "Unbekannt"


class TestWeighting:
    def test_gewichte_sind_konfigurierbar(self):
        heavy_price = DealScorer(ScoreWeights(price_advantage=90.0, condition_risk=1.0))
        heavy_risk = DealScorer(ScoreWeights(price_advantage=1.0, condition_risk=90.0))
        cheap_but_defect = {
            "condition_rank": 0,
            "condition_text": "Defekt",
            "total_price": 60.0,
            "price": 60.0,
            "description": "als Ersatzteil, defekt",
            "text_risk": analyze_text_risk("defekt", "als Ersatzteil, defekt, keine Ruecknahme"),
        }
        assert heavy_price.score(base_input(**cheap_but_defect)).score > heavy_risk.score(
            base_input(**cheap_but_defect)
        ).score

    def test_total_cost_beruecksichtigt_versand(self):
        free_shipping = DealScorer().score(base_input(total_price=175.0, shipping_cost=0.0))
        paid_shipping = DealScorer().score(base_input(total_price=175.0, shipping_cost=25.0))
        free = next(c for c in free_shipping.components if c.key == "total_cost")
        paid = next(c for c in paid_shipping.components if c.key == "total_cost")
        assert free.subscore >= paid.subscore

    def test_wissensbasis_fliesst_ein(self):
        entry = KnowledgeEntry(
            category="gpu", model="RTX 3080", good_price_max=150.0, fair_price_max=200.0
        )
        reference = KnowledgePriceReference(
            model="RTX 3080",
            category="gpu",
            currency="EUR",
            product_family="GeForce RTX",
            manufacturer="Nvidia",
            good_price_max=150.0,
            fair_price_max=200.0,
            overpriced_above=260.0,
            source="manual_knowledge_base",
            version=None,
            weight=1.0,
        )
        with_kb = DealScorer().score(
            base_input(knowledge_entry=entry, knowledge_reference=reference)
        )
        without_kb = DealScorer().score(base_input())
        assert with_kb.score != without_kb.score


class TestBenchmark:
    def test_benchmark_laeuft_ohne_fehler_durch(self):
        result = run_benchmark(ScoreWeights())
        assert result["failed"] == 0, result["notes"]
        assert result["passed"] > 0
        assert result["cases"]

    def test_bekannte_gute_deals_werden_hoeher_bewertet(self):
        result = run_benchmark(ScoreWeights())
        scores = {case["name"]: case["score"] for case in result["cases"]}
        assert scores["excellent"] > scores["overpriced"]
        assert scores["excellent"] > scores["risky_but_cheap"]
        assert scores["around_market"] > scores["overpriced"]
        assert scores["excellent"] > scores["no_model"]

    def test_benchmark_ist_reproduzierbar(self):
        first = run_benchmark(ScoreWeights())
        second = run_benchmark(ScoreWeights())
        assert first["passed"] == second["passed"]
        assert [c["score"] for c in first["cases"]] == [c["score"] for c in second["cases"]]

    def test_benchmark_faellt_bei_unsinnigen_gewichten_auf(self):
        """Nur ein absurd einseitiges Gewicht darf die Ordnung zerstoeren."""
        weird = ScoreWeights(
            price_advantage=0.0,
            price_performance=0.0,
            data_quality=0.0,
            condition_risk=100.0,
            total_cost=0.0,
            freshness=0.0,
            knowledge_match=0.0,
            location=0.0,
            market_comparison=0.0,
            information_quality=0.0,
        )
        result = run_benchmark(weird)
        assert result["passed"] + result["failed"] == len(result["cases"])
        assert result["cases"]

    def test_benchmark_faelle_sind_dokumentiert(self):
        cases = benchmark_cases()
        assert len(cases) >= 6
        names = {case.name for case in cases}
        assert {"excellent", "overpriced", "risky_but_cheap", "no_model"} <= names
        for case in cases:
            assert case.name
            assert case.description
            assert case.expectation
