"""Tests fuer die Hardware-Erkennung (Abschnitt 11)."""

from __future__ import annotations

import pytest

from backend.domain.hardware import (
    SOURCE_KNOWLEDGE_BASE,
    SOURCE_NONE,
    SOURCE_PATTERN,
    detect_manufacturer,
    models_ambiguous,
)


class TestManufacturer:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("ASUS RTX 3080", "Asus"),
            ("NVIDIA GeForce RTX 3080", "Nvidia"),
            ("Sapphire RX 6800 XT", "Sapphire"),
            ("irgendeine Grafikkarte", None),
        ],
    )
    def test_hersteller_aus_titel(self, text, expected):
        assert detect_manufacturer(text) == expected


class TestResolution:
    def test_voller_produktname(self, resolver):
        match = resolver.resolve("ASUS GeForce RTX 3080 TUF Gaming 10GB Grafikkarte")
        assert match.model == "RTX 3080"
        assert match.category == "gpu"
        assert match.variant == "10 GB"
        assert match.source == SOURCE_KNOWLEDGE_BASE
        assert match.confidence >= 0.8

    def test_abgekuerzte_schreibweise_ohne_leerzeichen(self, resolver):
        match = resolver.resolve("RTX3080Ti Grafikkarte")
        assert match.model == "RTX 3080 Ti"
        assert match.confidence >= 0.8

    def test_umgangssprachlicher_titel(self, resolver):
        match = resolver.resolve("Grafikkarte NVIDIA RTX 3080 10 GB - sehr guter Zustand")
        assert match.model == "RTX 3080"

    def test_modell_aus_beschreibung_statt_titel(self, resolver):
        match = resolver.resolve(
            "Gaming Grafikkarte abzugeben", "Es handelt sich um eine RTX 3080 von MSI."
        )
        assert match.model == "RTX 3080"
        # Treffer nur in der Beschreibung sind weniger sicher als im Titel.
        assert match.confidence < 0.8

    @pytest.mark.parametrize(
        ("title", "expected_model"),
        [
            ("AMD Ryzen 5 5600X Prozessor CPU", "Ryzen 5 5600X"),
            ("AMD Ryzen 5 5600 Prozessor", "Ryzen 5 5600"),
            ("Samsung 980 Pro 1TB NVMe SSD", "Samsung 980 Pro"),
            ("WD Black SN850X 2TB", "WD Black SN850X"),
            ("Corsair RM750x Netzteil 750W", "Corsair RMx"),
            ("Noctua NH-D15 CPU Kuehler", "Noctua NH-D15"),
        ],
    )
    def test_weitere_kategorien(self, resolver, title, expected_model):
        assert resolver.resolve(title).model == expected_model

    def test_kategorie_hinweis_wird_beachtet(self, resolver):
        assert resolver.resolve("RTX 3080", None, "gpu").model == "RTX 3080"
        # Falscher Hinweis darf nicht zu einer erfundenen Zuordnung fuehren.
        assert resolver.resolve("RTX 3080", None, "cpu").model is None

    def test_unbekanntes_modell_nutzt_mustererkennung(self, resolver):
        match = resolver.resolve("NVIDIA GeForce GTX 1070 8GB Grafikkarte")
        assert match.model == "GTX 1070"
        assert match.source == SOURCE_PATTERN
        assert match.confidence < 0.6
        assert match.notes  # Hinweis, dass die Zuordnung zu pruefen ist

    @pytest.mark.parametrize(
        "title",
        ["Grafikkarte laeuft, Zustand unklar", "PC Zubehoer Konvolut", "", None],
    )
    def test_kein_modell_wird_erfunden(self, resolver, title):
        match = resolver.resolve(title)
        assert match.model is None
        assert match.source == SOURCE_NONE
        assert match.confidence == 0.0


class TestVariants:
    def test_speichergroesse_als_variante(self, resolver):
        assert resolver.resolve("RX 6800 XT Grafikkarte 16GB").variant == "16 GB"

    def test_variantenname_wird_nicht_doppelt_angezeigt(self, resolver):
        """'RTX 4070 Ti' + Variante 'TI' waere eine Dopplung."""
        match = resolver.resolve("MSI RTX 4070 Ti Gaming X")
        assert match.model == "RTX 4070 Ti"
        assert match.variant is None

    def test_unterschiedliche_varianten_bleiben_unterscheidbar(self, resolver):
        ten = resolver.resolve("RTX 3080 10 GB Grafikkarte")
        twelve = resolver.resolve("RTX 3080 12 GB Grafikkarte")
        assert ten.model == twelve.model == "RTX 3080"
        assert ten.variant != twelve.variant


class TestAmbiguity:
    def test_genauere_bezeichnung_ist_nicht_mehrdeutig(self, resolver):
        """'RTX 3080 Ti' ist keine Konkurrenz zu 'RTX 3080'."""
        match = resolver.resolve("RTX 3080 Ti Grafikkarte")
        assert match.model == "RTX 3080 Ti"
        assert match.ambiguous is False
        assert not any("Mehrere Modelle" in note for note in match.notes)

    def test_echte_konkurrenz_senkt_die_sicherheit(self, resolver):
        match = resolver.resolve("RTX 3080 Ti oder RTX 4080 - Vergleich beider Karten")
        assert match.ambiguous is True
        assert match.confidence < 0.6

    def test_models_ambiguous_regel(self):
        assert models_ambiguous(["RTX 3080", "RTX 3080 Ti"]) is False
        assert models_ambiguous(["Ryzen 5 5600", "Ryzen 5 5600X"]) is False
        assert models_ambiguous(["RTX 3080", "RX 6800 XT"]) is True
        assert models_ambiguous(["RTX 3080", None]) is False
        assert models_ambiguous([]) is False
