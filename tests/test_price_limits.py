"""Tests für die Kauf-Limit-Liste (Höchstpreise.txt).

Die Liste ist handgepflegt und darf jederzeit erweitert werden. Deshalb wird
hier vor allem geprüft, wie sie gelesen wird: Abschnitte, Preisschreibweisen,
Suchbegriffe - und welche Anzeige welches Limit bekommt.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.domain.price_limits import (
    DEFAULT_PRICE_LIST_FILE,
    MIN_PRICE_RATIO,
    PriceList,
    load_price_list,
    minimum_price,
    parse_price_limits,
    search_term,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

SAMPLE = """
----------------------------------------------------------------
PC-KOMPONENTEN - GEBRAUCHTMARKT-KAUFLIMITS (Stand: 11.10.2026)
----------------------------------------------------------------
Die Zahl hinter dem Gleichheitszeichen ist der maximale Kaufpreis.
Einkalkuliert: Restteile fuer den Komplett-PC (Board, RAM, SSD, Netzteil).

----------------------------------------------------------------
GRAFIKKARTEN - NVIDIA
----------------------------------------------------------------
NVIDIA GeForce GTX 1080 = 65 €
NVIDIA GeForce GTX 1080 Ti = 100 €
NVIDIA GeForce RTX 3060 = 165 €
NVIDIA GeForce RTX 3060 Ti = 170 €
NVIDIA GeForce RTX 3080 10GB = 1.234,50 €
NVIDIA GeForce RTX 3080 12GB = 1.300 €

----------------------------------------------------------------
GRAFIKKARTEN - AMD
----------------------------------------------------------------
AMD Radeon RX 6600 XT = 115 €

----------------------------------------------------------------
CPUs - AMD (Ryzen)
----------------------------------------------------------------
AMD Ryzen 5 5600X = 70 €

----------------------------------------------------------------
HINWEISE
----------------------------------------------------------------
Varianten: RTX 3060 steht fuer die 12GB-Version.
Faustregel fuer Nachverhandlungen = 15 % unter dem Limit.
"""


@pytest.fixture()
def price_list() -> PriceList:
    return PriceList(entries=parse_price_limits(SAMPLE), path="beispiel.txt")


class TestParsen:
    def test_abschnitte_werden_zu_gruppen(self, price_list):
        groups = {entry.group for entry in price_list}
        assert groups == {"gpu", "cpu"}
        categories = {entry.category for entry in price_list}
        assert categories == {"gpu", "cpu"}

    def test_alle_eintraege_werden_gelesen(self, price_list):
        terms = [entry.term for entry in price_list]
        assert terms == [
            "GTX 1080",
            "GTX 1080 Ti",
            "RTX 3060",
            "RTX 3060 Ti",
            "RTX 3080 10GB",
            "RTX 3080 12GB",
            "RX 6600 XT",
            "Ryzen 5 5600X",
        ]

    def test_deutsche_und_englische_zahlen(self, price_list):
        by_term = {entry.term: entry.max_price for entry in price_list}
        assert by_term["RTX 3080 10GB"] == 1234.5
        assert by_term["RTX 3080 12GB"] == 1300.0
        assert by_term["GTX 1080"] == 65.0

    def test_hinweise_werden_nicht_zu_eintraegen(self, price_list):
        """Nach der Überschrift "HINWEISE" zählt keine Zeile mehr als Eintrag."""
        assert all("Faustregel" not in entry.name for entry in price_list)

    def test_eintrag_ohne_abschnitt_wird_ignoriert(self):
        text = "Ohne Abschnitt = 10 €\n"
        assert parse_price_limits(text) == []

    def test_kaputte_zeilen_werden_uebersprungen(self):
        text = (
            "GRAFIKKARTEN\n"
            "----------------------------------------------------------------\n"
            "NVIDIA GeForce RTX 3080 = 270 €\n"
            "NVIDIA GeForce RTX 3090 = auf Anfrage\n"
            "NVIDIA GeForce RTX 3070 = \n"
        )
        entries = parse_price_limits(text)
        assert [entry.term for entry in entries] == ["RTX 3080"]

    def test_gruppen_liefern_spanne_und_anzahl(self, price_list):
        groups = {group.key: group for group in price_list.groups()}
        assert groups["gpu"].count == 7
        assert groups["gpu"].min_price == 65.0
        assert groups["gpu"].max_price == 1300.0
        assert groups["gpu"].label == "Grafikkarten"
        assert groups["cpu"].count == 1


class TestMindestpreis:
    """Der Mindestpreis eines Produkts ist ein Viertel seines Höchstpreises."""

    @pytest.mark.parametrize(
        ("max_price", "expected"),
        [(240.0, 60.0), (270.0, 67.5), (245.0, 61.25), (1.0, 0.25)],
    )
    def test_viertel_des_hoechstpreises(self, max_price, expected):
        assert minimum_price(max_price) == pytest.approx(expected)
        assert minimum_price(max_price) == pytest.approx(max_price * MIN_PRICE_RATIO)

    def test_ohne_hoechstpreis_gibt_es_keinen_mindestpreis(self):
        """Ohne Grenze nach oben gibt es auch keine nach unten - nicht raten."""
        assert minimum_price(None) is None

    def test_limit_bekommt_einen_mindestpreis(self, price_list):
        limit = price_list.limit_for(title="ASUS RTX 3060 12GB")
        assert minimum_price(limit.max_price) == 41.25


class TestSuchbegriff:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("NVIDIA GeForce RTX 3080 10GB", "RTX 3080 10GB"),
            ("AMD Radeon RX 6600 XT", "RX 6600 XT"),
            ("AMD Ryzen 7 5800X3D", "Ryzen 7 5800X3D"),
            ("Intel Core i5-12400F", "i5-12400F"),
            ("Intel Core Ultra 5 245K", "Core Ultra 5 245K"),
            ("Intel Arc B580", "Arc B580"),
            ("Corsair Vengeance 32GB DDR4", "Corsair Vengeance 32GB DDR4"),
        ],
    )
    def test_herstellerpraefix_faellt_weg(self, name, expected):
        assert search_term(name) == expected


class TestLimitZuordnung:
    def test_laengster_treffer_gewinnt(self, price_list):
        assert price_list.limit_for(title="RTX 3060 Ti Gaming X").term == "RTX 3060 Ti"
        assert price_list.limit_for(title="GeForce RTX 3060 12GB").term == "RTX 3060"
        assert price_list.limit_for(title="GTX 1080 Ti OC").term == "GTX 1080 Ti"
        assert price_list.limit_for(title="GTX 1080 Gaming").term == "GTX 1080"

    def test_ohne_variantenangabe_gilt_das_strengste_limit(self, price_list):
        """Ohne Angabe der Speichergröße wird nicht schöngerechnet."""
        limit = price_list.limit_for(title="MSI RTX 3080 Gaming X")
        assert limit.term == "RTX 3080 10GB"
        assert limit.max_price == 1234.5

    def test_variante_wird_erkannt(self, price_list):
        assert price_list.limit_for(title="RTX 3080 12GB Karte").max_price == 1300.0

    def test_unbekannte_komponente_hat_kein_limit(self, price_list):
        assert price_list.limit_for(title="Samsung 990 Pro 2TB SSD") is None
        assert price_list.allows(title="Samsung 990 Pro 2TB SSD", price=500.0) is True

    def test_limit_gilt_inkl_versand(self, price_list):
        limit = price_list.limit_for(title="ASUS RTX 3060 12GB")
        assert limit.max_price == 165.0
        assert price_list.allows(title="ASUS RTX 3060 12GB", price=160.0) is True
        assert price_list.allows(title="ASUS RTX 3060 12GB", price=165.0) is True
        assert price_list.allows(title="ASUS RTX 3060 12GB", price=169.99) is False

    def test_ohne_preis_gilt_kein_limit(self, price_list):
        assert price_list.allows(title="RTX 3060", price=None) is True

    def test_warenzeichen_im_titel_stoert_das_limit_nicht(self, price_list):
        """„RTX™ 3060 Ti“ ist dieselbe Karte wie „RTX 3060 Ti“.

        Ohne diese Behandlung bliebe die Anzeige ohne Limit - ein Angebot weit
        über dem Kauf-Limit würde dann gespeichert und angezeigt.
        """
        titel = "MSI GeForce RTX™ 3060 Ti Gaming X Trio 8GB"
        limit = price_list.limit_for(title=titel)
        assert limit is not None
        assert limit.term == "RTX 3060 Ti"
        assert limit.max_price == 170.0
        assert price_list.allows(title=titel, price=169.0) is True
        assert price_list.allows(title=titel, price=400.0) is False

    def test_modell_und_variante_zaehlen_mit(self, price_list):
        """Der aufgelöste Modellname hilft, wenn der Titel ungenau ist."""
        assert (
            price_list.limit_for(title="Grafikkarte", model="RTX 3080", variant="12 GB").term
            == "RTX 3080 12GB"
        )


class TestModellfamilien:
    """Für die Sammelanzeige zählt das Modell, nicht die Ausbaustufe."""

    def test_ausbaustufen_eines_modells_bilden_eine_familie(self, price_list):
        """"RTX 3080 Ti 12GB" ist ein Modell, obwohl die Liste zwei Einträge führt."""
        familien = price_list.model_families_in_title(
            "MSI RTX 3080 Ti SUPRIM X 12GB GDDR6X Grafikkarte"
        )
        assert len(familien) == 1
        assert sorted(familien.values()) == ["RTX 3080"]

    def test_verschiedene_modelle_bleiben_getrennt(self, price_list):
        familien = price_list.model_families_in_title(
            "Grafikkarten: RTX 3060 Ti 2060 GTX 1080 1060 RX 6600 uvm"
        )
        assert len(familien) >= 3
        assert "GTX 1080" in familien.values()
        assert "RTX 3060 Ti" in familien.values()


class TestLaden:
    def test_datei_wird_gelesen_und_bei_aenderung_neu_geladen(self, tmp_path):
        path = tmp_path / "hoechstpreise.txt"
        path.write_text(
            "GRAFIKKARTEN\n"
            "----------------------------------------------------------------\n"
            "NVIDIA GeForce RTX 3080 = 270 €\n",
            encoding="utf-8",
        )
        first = load_price_list(path)
        assert [entry.term for entry in first] == ["RTX 3080"]

        path.write_text(
            "GRAFIKKARTEN\n"
            "----------------------------------------------------------------\n"
            "NVIDIA GeForce RTX 3080 = 250 €\n"
            "AMD Radeon RX 6600 = 100 €\n",
            encoding="utf-8",
        )
        second = load_price_list(path)
        assert len(second) == 2
        assert second.limit_for(title="RTX 3080 10GB").max_price == 250.0

    def test_fehlende_datei_ist_kein_fehler(self, tmp_path):
        price_list = load_price_list(tmp_path / "gibtsnicht.txt")
        assert price_list.available is False
        assert price_list.errors == []

    def test_leerer_pfad_schaltet_die_liste_ab(self):
        assert load_price_list("").available is False

    def test_datei_ohne_komponenten_erklaert_sich(self, tmp_path):
        path = tmp_path / "leer.txt"
        path.write_text("Nur ein Hinweis ohne Abschnitt.\n", encoding="utf-8")
        price_list = load_price_list(path)
        assert price_list.available is False
        assert price_list.errors

    def test_die_liste_des_projekts_ist_lesbar(self):
        """Die Datei, auf die die Anwendung standardmäßig zeigt, muss passen."""
        path = REPO_ROOT / DEFAULT_PRICE_LIST_FILE
        assert path.is_file(), f"{DEFAULT_PRICE_LIST_FILE} fehlt im Projektordner"
        price_list = load_price_list(path)
        assert price_list.available is True
        assert {"gpu", "cpu"} <= price_list.group_keys()
        for entry in price_list:
            assert entry.term
            assert entry.max_price > 0
