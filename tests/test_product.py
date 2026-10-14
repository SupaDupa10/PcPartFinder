"""Deutung eines freien Produktnamens (``backend/domain/product.py``).

Der Benutzer gibt nur noch einen konkreten Produktnamen und ein Budget an.
Diese Tests belegen, dass daraus die richtige Kategorie, passende
Modelle und ein belastbarer Relevanzfilter abgeleitet werden.
"""

from __future__ import annotations

from backend.domain.product import (
    FALLBACK_CATEGORY,
    interpret_product,
    product_tokens,
)


class TestProduktTokens:
    def test_generische_woerter_werden_entfernt(self):
        assert product_tokens("GPU Grafikkarte RTX 3080 gebraucht") == ["rtx", "3080"]

    def test_zusammengeschriebene_bezeichnung_bleibt_erhalten(self):
        assert product_tokens("Ryzen 5 5600X") == ["ryzen", "5600x"]

    def test_leerer_name(self):
        assert product_tokens("") == []
        assert product_tokens(None) == []


class TestDeutung:
    def test_grafikkarte_wird_als_gpu_erkannt(self, knowledge):
        result = interpret_product("RTX 3080 Ti", knowledge)
        assert result is not None
        assert result.categories[0] == "gpu"
        assert "RTX 3080 Ti" in result.matched_models
        assert result.match_quality == "model"

    def test_prozessor_wird_als_cpu_erkannt(self, knowledge):
        result = interpret_product("Ryzen 5 5600X", knowledge)
        assert result is not None
        assert result.categories[0] == "cpu"
        assert result.matched_models

    def test_unbekanntes_produkt_faellt_auf_breite_suche_zurueck(self, knowledge):
        result = interpret_product("Schallplattenspieler Thorens TD 160", knowledge)
        assert result is not None
        assert result.categories == [FALLBACK_CATEGORY]
        assert result.match_quality == "none"
        # Auch ohne bekannte Hardware bleibt der Name der Suchbegriff.
        assert result.required_tokens == ["schallplattenspieler", "thorens", "td", "160"]

    def test_relevanzfilter_verlangt_zwei_bestandteile(self, knowledge):
        result = interpret_product("RTX 3080 Ti", knowledge)
        assert result is not None
        assert result.required_min_matches == 2
        assert result.to_dict()["required_min_matches"] == 2

    def test_einzelner_bestandteil_ist_zwingend(self, knowledge):
        result = interpret_product("GTX1080", knowledge)
        assert result is not None
        assert result.required_tokens == ["gtx1080"]
        assert result.required_min_matches == 1

    def test_ohne_namen_keine_deutung(self, knowledge):
        assert interpret_product("   ", knowledge) is None
        assert interpret_product(None, knowledge) is None

    def test_hoechstens_zwei_kategorien(self, knowledge):
        result = interpret_product("Ryzen CPU Mainboard RTX 3080", knowledge)
        assert result is not None
        assert len(result.categories) <= 2

    def test_ohne_wissensbasis_bleibt_die_kategorie_regelbasiert(self):
        result = interpret_product("Grafikkarte RTX 3080", None)
        assert result is not None
        assert result.categories[0] == "gpu"
        assert result.matched_models == []
