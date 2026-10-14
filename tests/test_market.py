"""Tests fuer die Marktpreisermittlung (Abschnitt 10)."""

from __future__ import annotations

import textwrap
from datetime import datetime, timedelta, timezone

import pytest

from backend.knowledge.base import KnowledgeBase
from backend.models import (
    FORMAT_FIXED_PRICE,
    FORMAT_UNKNOWN,
    STATUS_ACTIVE,
    STATUS_GONE,
    Listing,
)
from backend.pricing.market import (
    MarketPriceEstimator,
    describe_uncertainty,
    remove_outliers,
)

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)


def write_kb(tmp_path, body: str) -> KnowledgeBase:
    path = tmp_path / "kb.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    base = KnowledgeBase()
    base.load([path])
    return base


def add_listing(
    session,
    *,
    model: str = "RTX 3080",
    category: str = "gpu",
    total: float | None = 250.0,
    condition_rank: int | None = 2,
    age_days: int = 10,
    listing_format: str = FORMAT_FIXED_PRICE,
    status: str = STATUS_ACTIVE,
) -> Listing:
    observed = NOW - timedelta(days=age_days)
    listing = Listing(
        platform="test",
        platform_listing_id=None,
        url=f"https://example.invalid/{model}/{total}/{age_days}/{id(object())}",
        title=f"{model} Testangebot",
        price=total,
        shipping_cost_known=True,
        total_price=total,
        category=category,
        model=model,
        condition_rank=condition_rank,
        listing_format=listing_format,
        status=status,
        fetched_at=observed,
        last_seen_at=observed,
    )
    session.add(listing)
    session.flush()
    return listing


@pytest.fixture()
def empty_kb(tmp_path):
    return write_kb(tmp_path, "entries:\n  - category: gpu\n    model: RTX 3080\n")


class TestOutliers:
    def test_kleine_stichprobe_wird_nicht_gefiltert(self):
        values = [1.0, 1000.0, 1200.0]
        kept, removed = remove_outliers(values)
        assert kept == values
        assert removed == 0

    def test_extremer_ausreisser_wird_entfernt(self):
        values = [200.0, 210.0, 220.0, 230.0, 240.0, 5000.0]
        kept, removed = remove_outliers(values)
        assert 5000.0 not in kept
        assert removed == 1

    def test_identische_werte_bleiben_unveraendert(self):
        values = [100.0] * 6
        kept, removed = remove_outliers(values)
        assert kept == values
        assert removed == 0


class TestNoData:
    def test_ohne_daten_kein_erfundener_marktwert(self, empty_kb):
        estimator = MarketPriceEstimator(empty_kb)
        estimate = estimator.estimate(None, category="gpu", model="RTX 3080")
        assert estimate.value is None
        assert estimate.method == "algorithmic_estimate"
        assert estimate.uncertain is True
        assert estimate.uncertainty == 1.0
        assert describe_uncertainty(estimate) == "Kein Marktwert bestimmbar"

    def test_ohne_kategorie_oder_modell_keine_schaetzung(self, empty_kb):
        estimator = MarketPriceEstimator(empty_kb)
        assert estimator.estimate(None, category=None, model=None).value is None

    def test_zu_wenige_beobachtungen_ergeben_keinen_belastbaren_wert(
        self, empty_kb, db_session
    ):
        add_listing(db_session, total=200.0)
        add_listing(db_session, total=210.0)
        estimator = MarketPriceEstimator(empty_kb, min_observations=5)
        estimate = estimator.estimate(db_session, category="gpu", model="RTX 3080")
        assert estimate.value is None  # unter MIN_OBSERVATIONS_FOR_ANY_ESTIMATE
        assert estimate.observations == 2
        assert any("zu wenig" in note for note in estimate.notes)

    def test_einzelne_beobachtung_wird_als_unsicher_gekennzeichnet(
        self, empty_kb, db_session
    ):
        add_listing(db_session, total=200.0)
        estimator = MarketPriceEstimator(empty_kb, min_observations=5)
        estimate = estimator.estimate(db_session, category="gpu", model="RTX 3080")
        assert estimate.value is None
        assert estimate.uncertainty == 1.0


class TestObservedMarket:
    def test_median_statt_durchschnitt(self, empty_kb, db_session):
        for price in (150.0, 200.0, 210.0, 220.0, 230.0, 600.0):
            add_listing(db_session, total=price)
        estimator = MarketPriceEstimator(empty_kb, min_observations=5)
        estimate = estimator.estimate(db_session, category="gpu", model="RTX 3080")
        assert estimate.value is not None
        # Der Median liegt deutlich unter dem arithmetischen Mittel (268,33 EUR).
        assert estimate.value < 240.0
        assert estimate.median is not None
        assert estimate.q1 is not None and estimate.q3 is not None
        assert estimate.q1 <= estimate.median <= estimate.q3
        assert estimate.method.startswith("observed_median")

    def test_ausreisser_werden_entfernt_und_gemeldet(self, empty_kb, db_session):
        for price in (200.0, 205.0, 210.0, 215.0, 220.0, 9000.0):
            add_listing(db_session, total=price)
        estimator = MarketPriceEstimator(empty_kb, min_observations=5)
        estimate = estimator.estimate(db_session, category="gpu", model="RTX 3080")
        assert estimate.outliers_removed == 1
        assert estimate.value is not None and estimate.value < 300.0

    def test_nur_passende_angebote_zaehlen(self, empty_kb, db_session):
        add_listing(db_session, model="RTX 3080", total=200.0)
        add_listing(db_session, model="RX 6800 XT", total=180.0)
        add_listing(db_session, model="RTX 3080", total=210.0, listing_format=FORMAT_UNKNOWN)
        add_listing(db_session, model="RTX 3080", total=190.0, status=STATUS_GONE)
        estimator = MarketPriceEstimator(empty_kb, min_observations=3)
        estimate = estimator.estimate(db_session, category="gpu", model="RTX 3080")
        assert estimate.observations == 1

    def test_alte_beobachtungen_fallen_aus_dem_fenster(self, empty_kb, db_session):
        for price in (200.0, 210.0, 220.0, 230.0, 240.0):
            add_listing(db_session, total=price, age_days=400)
        estimator = MarketPriceEstimator(empty_kb, min_observations=5, window_days=150)
        estimate = estimator.estimate(db_session, category="gpu", model="RTX 3080", now=NOW)
        assert estimate.observations == 0

    def test_aktuelle_beobachtungen_zaehlen(self, empty_kb, db_session):
        for price in (200.0, 210.0, 220.0, 230.0, 240.0):
            add_listing(db_session, total=price, age_days=5)
        estimator = MarketPriceEstimator(empty_kb, min_observations=5, window_days=150)
        estimate = estimator.estimate(db_session, category="gpu", model="RTX 3080", now=NOW)
        assert estimate.observations == 5
        assert estimate.freshest_at is not None
        assert estimate.value is not None

    def test_kleine_stichprobe_bleibt_als_unsicher_markiert(self, empty_kb, db_session):
        """Fuenf Beobachtungen sind eine schwache Datenlage - das muss sichtbar sein."""
        for price in (200.0, 210.0, 220.0, 230.0, 240.0):
            add_listing(db_session, total=price, age_days=5)
        estimator = MarketPriceEstimator(empty_kb, min_observations=5, window_days=150)
        estimate = estimator.estimate(db_session, category="gpu", model="RTX 3080", now=NOW)
        assert estimate.uncertain is True

    def test_grosse_stichprobe_ist_belastbar(self, empty_kb, db_session):
        for index in range(20):
            add_listing(db_session, total=200.0 + index, age_days=index + 1)
        estimator = MarketPriceEstimator(empty_kb, min_observations=5, window_days=150)
        estimate = estimator.estimate(db_session, category="gpu", model="RTX 3080", now=NOW)
        assert estimate.observations == 20
        assert estimate.uncertain is False
        assert describe_uncertainty(estimate) == "Marktwert belastbar"

    def test_eigenes_angebot_wird_ausgeschlossen(self, empty_kb, db_session):
        own = add_listing(db_session, total=999.0)
        for price in (200.0, 205.0, 210.0, 215.0, 220.0):
            add_listing(db_session, total=price)
        estimator = MarketPriceEstimator(empty_kb, min_observations=5)
        estimate = estimator.estimate(
            db_session, category="gpu", model="RTX 3080", exclude_listing_id=own.id
        )
        assert estimate.observations == 5
        assert estimate.value is not None and estimate.value < 250.0


class TestKnowledgeReference:
    def test_preiswissen_ohne_beobachtungen(self, tmp_path):
        kb = write_kb(
            tmp_path,
            """
            entries:
              - category: gpu
                model: RTX 3080
                fair_price_max: 220
                good_price_max: 180
                overpriced_above: 260
            """,
        )
        estimate = MarketPriceEstimator(kb).estimate(None, category="gpu", model="RTX 3080")
        assert estimate.value == 220.0
        assert estimate.method == "knowledge_base_reference"
        assert "manual_knowledge_base" in estimate.used_sources
        assert any("Preiswissen" in note for note in estimate.notes)

    def test_wissensbasis_und_beobachtungen_werden_getrennt_ausgewiesen(
        self, tmp_path, db_session
    ):
        kb = write_kb(
            tmp_path,
            """
            entries:
              - category: gpu
                model: RTX 3080
                fair_price_max: 300
                weight: 1.0
            """,
        )
        for price in (200.0, 210.0, 220.0, 230.0, 240.0):
            add_listing(db_session, total=price)
        estimate = MarketPriceEstimator(kb, min_observations=5).estimate(
            db_session, category="gpu", model="RTX 3080"
        )
        assert estimate.method == "combined_median_and_knowledge"
        assert set(estimate.used_sources) == {"observed_offers", "manual_knowledge_base"}
        assert any("Kombination" in note for note in estimate.notes)
        assert estimate.knowledge_reference is not None

    def test_reiner_wissenswert_wird_nie_als_exakt_dargestellt(self, tmp_path, db_session):
        """Ein gepflegter Richtwert ist belastbarer als nichts, aber nicht exakt."""
        kb = write_kb(
            tmp_path, "entries:\n  - category: gpu\n    model: RTX 3080\n    fair_price_max: 200\n"
        )
        estimate = MarketPriceEstimator(kb).estimate(db_session, category="gpu", model="RTX 3080")
        assert estimate.value == 200.0
        assert estimate.used_sources == ["manual_knowledge_base"]
        assert 0.25 < estimate.uncertainty < 0.5
        assert describe_uncertainty(estimate) == "Marktwert mäßig belastbar"


class TestConditionAdjustment:
    def test_zustand_wird_normiert(self, empty_kb, db_session):
        for rank in (2, 2, 2, 2, 2):
            add_listing(db_session, total=200.0, condition_rank=rank)
        estimator = MarketPriceEstimator(empty_kb, min_observations=5)
        good = estimator.estimate(
            db_session, category="gpu", model="RTX 3080", condition_rank=2
        )
        new = estimator.estimate(
            db_session, category="gpu", model="RTX 3080", condition_rank=4
        )
        assert good.value is not None and new.value is not None
        # Ein neuwertiges Angebot hat einen hoeheren Marktwert als ein gutes.
        assert new.value > good.value

    def test_basis_enthaelt_quellenangabe_und_hinweis(self, empty_kb, db_session):
        for price in (200.0, 210.0, 220.0, 230.0, 240.0):
            add_listing(db_session, total=price)
        estimate = MarketPriceEstimator(empty_kb, min_observations=5).estimate(
            db_session, category="gpu", model="RTX 3080"
        )
        basis = estimate.to_basis()
        assert basis["observations"] == 5
        assert basis["used_sources"]
        assert "keine erzielten Verkaufspreise" in basis["disclaimer"]
