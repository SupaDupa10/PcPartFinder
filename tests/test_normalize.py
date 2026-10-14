"""Tests fuer Normalisierung, Preisformate, Zustand und Standortlogik."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from backend.domain.normalize import (
    CONDITION_ACCEPTABLE,
    CONDITION_DEFECT,
    CONDITION_GOOD,
    CONDITION_NEW,
    CONDITION_VERY_GOOD,
    age_days,
    compact_key,
    compute_total_price,
    condition_rank_from_id,
    condition_rank_from_text,
    content_hash,
    match_key_contains,
    normalize_postal_code,
    normalize_text,
    parse_datetime,
    parse_price,
    pattern_present,
    postcode_distance_km,
    resolve_condition_rank,
    searchable_condition_ranks,
)


class TestParsePrice:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("1.234,56", 1234.56),
            ("1,234.56", 1234.56),
            ("250", 250.0),
            ("250,00", 250.0),
            ("250.00", 250.0),
            ("250 €", 250.0),
            ("EUR 250", 250.0),
            ("1.234", 1234.0),  # einzelner Punkt mit 3 Stellen = Tausendertrenner
            ("1.234.567", 1234567.0),
            ("0", 0.0),
            (199.99, 199.99),
            ("VB 185,50 EUR", 185.5),
        ],
    )
    def test_deutsche_und_englische_formate(self, raw, expected):
        assert parse_price(raw) == pytest.approx(expected)

    @pytest.mark.parametrize("raw", [None, "", "kostenlos", "Preis auf Anfrage", "VB", True, -5])
    def test_unklare_werte_liefern_none(self, raw):
        """Ein unklarer Preis wird nie geraten."""
        assert parse_price(raw) is None


class TestTotalPrice:
    def test_versand_bekannt_wird_addiert(self):
        assert compute_total_price(100.0, 5.0, True) == 105.0

    def test_versand_unbekannt_wird_nicht_als_null_gewertet(self):
        """Fehlende Versandkosten duerfen den Gesamtpreis nicht schoenrechnen."""
        total = compute_total_price(100.0, None, False)
        assert total == 100.0

    def test_abholung_erlaubt_gleichstand_ohne_versand(self):
        assert compute_total_price(100.0, None, False, pickup_available=True) == 100.0

    def test_ohne_preis_kein_gesamtpreis(self):
        assert compute_total_price(None, 5.0, True) is None


class TestCondition:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Neu", CONDITION_NEW),
            ("Neu mit OVP", CONDITION_NEW),
            ("Versiegelt", CONDITION_NEW),
            ("Sehr gut", CONDITION_VERY_GOOD),
            ("Wie neu", CONDITION_VERY_GOOD),
            ("Neuwertig", CONDITION_VERY_GOOD),
            ("Gebraucht, gut", CONDITION_GOOD),
            ("Gebraucht", CONDITION_GOOD),
            ("Akzeptabel", CONDITION_ACCEPTABLE),
            ("Als Ersatzteil / defekt", CONDITION_DEFECT),
            ("Defekt, Bastler", CONDITION_DEFECT),
        ],
    )
    def test_zustandstexte(self, text, expected):
        assert condition_rank_from_text(text) == expected

    def test_qualitaetsnote_schlaegt_nutzungsklasse(self):
        """"Gebraucht, sehr gut" ist Rang 3 - nicht Rang 2 wie das bloße "gebraucht"."""
        assert condition_rank_from_text("Gebraucht, sehr gut") == CONDITION_VERY_GOOD
        assert condition_rank_from_text("Gebraucht: Sehr gut") == CONDITION_VERY_GOOD
        assert condition_rank_from_text("Gebraucht, gut") == CONDITION_GOOD

    def test_sehr_gut_refurbished_schlaegt_gut_refurbished(self):
        assert condition_rank_from_text("Sehr gut - Refurbished") == CONDITION_VERY_GOOD
        assert condition_rank_from_text("Gut - Refurbished") == CONDITION_GOOD

    def test_verneinter_defekt_ist_kein_defekt(self):
        assert condition_rank_from_text("Nicht defekt. Gebraucht.") == CONDITION_GOOD
        assert condition_rank_from_text("kein Defekt, laeuft einwandfrei") is None

    @pytest.mark.parametrize("text", [None, "", "Zustand unklar", "siehe Beschreibung"])
    def test_unbekannter_zustand(self, text):
        assert condition_rank_from_text(text) is None

    def test_id_als_rueckfall_aber_text_hat_vorrang(self):
        assert resolve_condition_rank(None, "3000") == CONDITION_GOOD
        assert resolve_condition_rank("Neu", "3000") == CONDITION_NEW
        assert resolve_condition_rank(None, None) is None
        assert condition_rank_from_id("unbekannt") is None


class TestNegation:
    def test_verneintes_signal_zaehlt_nicht(self):
        assert pattern_present("kein Spulenfiepen vorhanden", "spulenfiepen") is False
        assert pattern_present("ohne Garantie", "garantie") is False
        assert pattern_present("keine Kratzer", "kratzer") is False

    def test_normales_signal_zaehlt(self):
        assert pattern_present("deutliches Spulenfiepen", "spulenfiepen") is True
        assert pattern_present("Garantie bis 2027", "garantie") is True

    def test_verneinung_wirkt_nicht_ueber_satzgrenze(self):
        assert pattern_present("kein Netzteil. Spulenfiepen vorhanden", "spulenfiepen") is True

    def test_mehrfachvorkommen_zaehlt_wenn_eines_nicht_verneint_ist(self):
        text = "kein Spulenfiepen bei Last, aber Spulenfiepen im Leerlauf"
        assert pattern_present(text, "spulenfiepen") is True


class TestDatetime:
    def test_iso_mit_z(self):
        assert parse_datetime("2026-10-02T09:15:00Z") == datetime(
            2026, 10, 2, 9, 15, tzinfo=timezone.utc
        )

    def test_posix_zeitstempel(self):
        assert parse_datetime(1_700_000_000) == datetime.fromtimestamp(
            1_700_000_000, tz=timezone.utc
        )

    @pytest.mark.parametrize("raw", [None, "", "irgendwann", "32.13.2026", object()])
    def test_unbekanntes_datum_liefert_none(self, raw):
        """Ein unbekanntes Datum muss als unbekannt erhalten bleiben."""
        assert parse_datetime(raw) is None

    def test_alter_bei_unbekanntem_datum(self):
        assert age_days(None) is None
        now = datetime(2026, 10, 10, tzinfo=timezone.utc)
        assert age_days(datetime(2026, 10, 5, tzinfo=timezone.utc), now) == pytest.approx(5.0)


class TestLocation:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("80331", "80331"), ("8033", "8033"), ("PLZ 10115", "10115"), ("", None), ("abc", None)],
    )
    def test_plz_normalisierung(self, raw, expected):
        assert normalize_postal_code(raw) == expected

    def test_entfernung_ist_grob_und_ehrlich(self):
        assert postcode_distance_km("10115", "80331") == pytest.approx(7 * 110.0)
        assert postcode_distance_km("10115", "10117") == 0.0

    def test_ohne_daten_keine_erfundene_entfernung(self):
        assert postcode_distance_km(None, "80331") is None
        assert postcode_distance_km("", "") is None
        assert postcode_distance_km("ab", "cd") is None


class TestSuchbareZustaende:
    """Die Suche ist auf gebrauchte, funktionierende Hardware ausgelegt."""

    def test_gebrauchte_zustaende_ohne_defekt_und_neuware(self):
        assert searchable_condition_ranks() == [
            CONDITION_ACCEPTABLE,
            CONDITION_GOOD,
            CONDITION_VERY_GOOD,
        ]

    def test_mindestrang_wird_angewendet(self):
        assert searchable_condition_ranks(CONDITION_GOOD) == [
            CONDITION_GOOD,
            CONDITION_VERY_GOOD,
        ]

    def test_defekt_und_neuware_werden_nie_gesucht(self):
        assert CONDITION_DEFECT not in searchable_condition_ranks(CONDITION_DEFECT)
        assert CONDITION_NEW not in searchable_condition_ranks(CONDITION_NEW)


class TestTextHelpers:
    def test_umlaute_werden_aufgeloest(self):
        assert normalize_text("Sehr Gut, Gebraucht!") == "sehr gut, gebraucht!"
        # Umlaute werden deutsch transliteriert (ö -> oe), nicht akzentfrei entfernt.
        assert normalize_text("Größe/Übertaktung") == "groesse/uebertaktung"

    def test_compact_key_ignoriert_formatierung(self):
        assert compact_key("RTX 3080 10GB") == compact_key("rtx-3080  10gb")

    def test_match_key_contains_akzeptiert_abkuerzung(self):
        assert match_key_contains("ASUS RTX3080Ti OC", "rtx 3080 ti") is True
        assert match_key_contains("RX580 8GB", "rx 580") is True
        assert match_key_contains("RX5800 Karte", "rx 580") is False

    def test_content_hash_ist_stabil(self):
        assert content_hash("a", "b") == content_hash("a", "b")
        assert content_hash("a", "b") != content_hash("a", "c")
        assert len(content_hash("a")) == 64
