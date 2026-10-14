"""Tests fuer die Wissensbasis (Abschnitt 9), inkl. unvollstaendiger Dateien."""

from __future__ import annotations

import json
import textwrap

import pytest

from backend.knowledge.base import KnowledgeBase


def write(path, content: str):
    path.write_text(textwrap.dedent(content), encoding="utf-8")
    return path


@pytest.fixture()
def priced_yaml(tmp_path):
    """Wissensbasis mit gepflegten Preisen plus einem Eintrag ohne Preise."""
    return write(
        tmp_path / "kb.yaml",
        """
        version: "2026-10"
        entries:
          - category: gpu
            model: "Beispielmodell"
            manufacturer: "Beispiel"
            aliases: ["Beispiel Modell 3000"]
            currency: EUR
            good_price_max: 150
            fair_price_max: 190
            overpriced_above: 220
            notes: ["Testnotiz"]
          - category: cpu
            model: "Identitaetsmodell"
            manufacturer: "Ohne Preise"
        """,
    )


class TestLoading:
    def test_preise_und_identitaeten_werden_getrennt_gezaehlt(self, priced_yaml):
        base = KnowledgeBase()
        summary = base.load([priced_yaml])
        assert summary.entries == 2
        assert summary.priced_entries == 1
        assert summary.identity_only_entries == 1
        assert summary.errors == []
        assert summary.per_category == {"cpu": 1, "gpu": 1}

    def test_identitaetseintrag_meldet_fehlende_preise(self, priced_yaml):
        base = KnowledgeBase()
        base.load([priced_yaml])
        entry = base.find_entry("cpu", "Identitaetsmodell")
        assert entry is not None
        assert entry.is_identity_only is True
        assert base.find_price_reference("cpu", "Identitaetsmodell") is not None

    def test_preisrichtwert_wird_gefunden(self, priced_yaml):
        base = KnowledgeBase()
        base.load([priced_yaml])
        reference = base.find_price_reference("gpu", "Beispielmodell")
        assert reference is not None
        assert reference.fair_price_max == 190
        assert reference.good_price_max == 150
        assert reference.target_value() == 190
        assert reference.source  # Herkunft ist immer nachvollziehbar
        assert reference.version == "2026-10"

    def test_alias_schreibweise_zaehlt(self, priced_yaml):
        base = KnowledgeBase()
        base.load([priced_yaml])
        assert base.find_price_reference("gpu", "Beispiel Modell 3000") is not None

    def test_keine_erfundenen_preise_ohne_eintrag(self, priced_yaml):
        base = KnowledgeBase()
        base.load([priced_yaml])
        assert base.find_price_reference("gpu", "Voellig anderes Modell") is None
        assert base.find_price_reference("", "") is None

    def test_json_format_wird_unterstuetzt(self, tmp_path):
        path = tmp_path / "kb.json"
        path.write_text(
            json.dumps({"entries": [{"category": "ram", "model": "DDR4 32 GB Kit"}]}),
            encoding="utf-8",
        )
        base = KnowledgeBase()
        summary = base.load([path])
        assert summary.entries == 1
        assert base.entries_for_category("ram")

    def test_einzelner_eintrag_ohne_liste(self, tmp_path):
        path = tmp_path / "single.yaml"
        write(path, "category: ssd\nmodel: Test SSD\n")
        base = KnowledgeBase()
        assert base.load([path]).entries == 1

    def test_markdown_notizen_werden_geladen(self, tmp_path):
        write(tmp_path / "notes.md", "# Hinweise\n\nNur Notizen, keine Preise.")
        write(tmp_path / "kb.yaml", "entries:\n  - category: gpu\n    model: X\n")
        base = KnowledgeBase()
        summary = base.load([tmp_path])
        assert summary.markdown_notes_available is True
        assert base.markdown_notes()


class TestRobustness:
    def test_defekte_datei_bricht_den_rest_nicht_ab(self, tmp_path):
        write(tmp_path / "broken.yaml", "entries: [ {category: gpu, model: [}")
        write(tmp_path / "ok.yaml", "entries:\n  - category: gpu\n    model: Gut\n")
        base = KnowledgeBase()
        summary = base.load([tmp_path])
        assert summary.entries == 1
        assert summary.errors and "broken.yaml" in summary.errors[0]

    def test_unbekannte_zusatzfelder_werden_erlaubt(self, tmp_path):
        path = tmp_path / "extra.yaml"
        write(
            path,
            """
            entries:
              - category: gpu
                model: Mit Extra
                zusatzfeld: bleibt erhalten
            """,
        )
        base = KnowledgeBase()
        base.load([path])
        assert base.entries[0].model == "Mit Extra"

    def test_fehleender_pfad_wird_gemeldet(self, tmp_path):
        base = KnowledgeBase()
        summary = base.load([tmp_path / "gibt-es-nicht.yaml"])
        assert summary.entries == 0
        assert summary.errors
        assert "nicht gefunden" in summary.errors[0]

    def test_leere_wissensbasis_ist_kein_fehler(self, tmp_path):
        base = KnowledgeBase()
        summary = base.load([write(tmp_path / "empty.yaml", "entries: []")])
        assert summary.entries == 0
        assert summary.errors == []
        assert base.entries == []
        assert base.preferred_terms("gpu") == []
        assert base.excluded_terms("gpu") == []

    def test_ungueltiger_eintrag_wird_uebersprungen(self, tmp_path):
        """Ein Eintrag ohne Pflichtfeld darf die Datei nicht unlesbar machen."""
        write(
            tmp_path / "kb.yaml",
            """
            entries:
              - category: gpu
                description: "kein model-Feld"
              - category: gpu
                model: Gueltig
            """,
        )
        base = KnowledgeBase()
        summary = base.load([tmp_path])
        assert summary.entries == 1
        assert summary.errors


class TestPreferenceAndExclusion:
    def test_problematische_modelle_werden_zu_ausschlussbegriffen(self, tmp_path):
        path = write(
            tmp_path / "kb.yaml",
            """
            entries:
              - category: gpu
                model: Mining Karte
                problematic: true
              - category: gpu
                model: Bevorzugt
                weight: 1.5
              - category: gpu
                model: Neutral
            """,
        )
        base = KnowledgeBase()
        base.load([path])
        assert base.excluded_terms("gpu") == ["Mining Karte"]
        assert base.preferred_terms("gpu") == ["Bevorzugt"]

    def test_suche_nach_text_und_kategorie(self, priced_yaml):
        base = KnowledgeBase()
        base.load([priced_yaml])
        assert [e.model for e in base.search("beispiel", "gpu")] == ["Beispielmodell"]
        assert base.search("beispiel", "cpu") == []
        assert base.search(None, "gpu") == base.entries_for_category("gpu")
