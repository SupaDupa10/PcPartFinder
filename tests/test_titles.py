"""Titelprüfung: Schlagwörter im Titel entscheiden - rein deterministisch.

Geprüft wird, dass Zubehör, Einzelteile und fremde Produkte ohne KI aussortiert
werden, echte Angebote aber unberührt bleiben, und dass jeder Titel genau ein
Urteil bekommt (es gibt kein "ohne Urteil" mehr).
"""

from __future__ import annotations

import pytest

from backend.domain.price_limits import PriceLimit, PriceList
from backend.search.titles import (
    ART_SAMMELANZEIGE,
    ART_TEIL,
    TitleFilter,
    TitleTarget,
    accessory_reason,
    keyword_match,
    match_product,
    target_text,
    title_key,
    title_product_reason,
)


class _Angebot:
    """Minimales Roh-Angebot: Titel und optionaler Beschreibungstext."""

    def __init__(self, title: str, description: str | None = None) -> None:
        self.title = title
        self.description = description


def _target(term: str = "RTX 3060", category: str | None = "gpu") -> TitleTarget:
    return TitleTarget.from_query(term=term, platform="ebay_web", category=category)


def _price_list(*terms: tuple[str, str] | str) -> PriceList:
    """Kauf-Limit-Liste aus Paaren ``(Begriff, Kategorie)`` oder bloss Begriffen."""
    entries = []
    for item in terms:
        term, category = item if isinstance(item, tuple) else (item, "gpu")
        entries.append(
            PriceLimit(
                group=category,
                group_label=category,
                category=category,
                name=term,
                term=term,
                max_price=300.0,
            )
        )
    return PriceList(entries=entries)


def filter_factory(**kwargs) -> TitleFilter:
    return TitleFilter(**kwargs)


class TestSchluessel:
    def test_gleicher_titel_gleicher_schluessel(self):
        assert title_key("ASUS RTX 3060 Grafikkarte") == title_key(
            "  asus   rtx 3060   grafikkarte "
        )

    def test_verschiedene_titel_verschiedene_schluessel(self):
        assert title_key("RTX 3060") != title_key("RTX 3070")

    def test_leerer_titel_hat_keinen_schluessel(self):
        assert title_key("   ") == ""


class TestSchlagwoerter:
    def test_kompaktform_zaehlt(self):
        assert keyword_match("ASUS RTX3080 Gaming", "RTX 3080") is True

    def test_wortfolge_zaehlt(self):
        assert keyword_match("ASUS GeForce RTX 3080 Gaming", "RTX 3080") is True

    def test_fehlende_bestandteile_zaehlen_nicht(self):
        assert keyword_match("ASUS RTX 3070 Gaming", "RTX 3080") is False

    def test_leerer_titel_ist_kein_treffer(self):
        assert keyword_match("", "RTX 3080") is False


class TestTeileregel:
    """Eindeutige Teil- und Zubehör-Titel - die Regel greift rein deterministisch.

    Genau diese Titel sind in der Praxis durchgelaufen und sollen hier hängen
    bleiben.
    """

    @pytest.mark.parametrize(
        "titel",
        [
            "1PCS / RTX2060 SUPER 8GB VENTUS GRAPHICS CARD FAN PLD09210S12HH",
            "Graka Lüfter Zotac RTX 2060 2070 Super Extreme 2080Ti AMP",
            "ASUS RTX 2080S Dual Lüfter Ersatzlüfter NEU",
            "GPU Halterung für RTX 2060 Grafikkarte, Stütze Bracket",
            "(FÜR ERSATZTEILE) Gigabyte GeForce RTX 4090 Gaming OC nur Platine ohne VRAM",
            "PCIe Riser Kabel für RTX 3060 Grafikkarte",
            "Wärmeleitpads für RTX 3060 Grafikkarte",
            # "Teil für Modell" über Hauptwort + Verbindung.
            "FD8015U12D VGA Fan for GTX1660Ti RTX2060 GAMING OC Graphics Card Cooling 4Pin",
            "Low Profile Full High Bracket For MSI GTX1050 GTX1050Ti LP Graphics Video Cards",
            "1Set GA91S2U Graphics Card Cooling VGA Coolers Fan 4Pin for Gainward GTX1660",
        ],
    )
    def test_eindeutige_teile_werden_erkannt(self, titel):
        assert accessory_reason(titel, vorgabe="RTX 3060 Grafikkarte") is not None

    @pytest.mark.parametrize(
        "titel",
        [
            "MSI GeForce RTX 3060 Ventus 6G OC Grafikkarte",
            "RTX 3060 6GB Grafikkarte, Lüfter gereinigt, läuft einwandfrei",
            "Gigabyte RTX 3060 Windforce, OVP und Rechnung dabei",
            # "Lüfter" und "Kühlkörper" stehen auch bei echten Karten im Titel.
            "EVGA NVIDIA GeForce RTX 2060 OC Grafikkarte PCIe x16 Lüfter mit Kühlkörper",
            "PNY GeForce GTX 1660 Ti OC 6GB | Arctic-Kühler – Lüfter",
            # "Fan" ohne Verbindung ist eine Eigenschaft der Karte.
            "ASUS GeForce RTX 3060 Dual Fan OC 12GB",
        ],
    )
    def test_echte_angebote_bleiben_unberuehrt(self, titel):
        assert accessory_reason(titel, vorgabe="RTX 3060 Grafikkarte") is None

    @pytest.mark.parametrize(
        "titel",
        [
            # Zubehör ohne Verbindung, inkl. Schreibvarianten und Tippfehler.
            "MSI GTX 1080 Waterblock",
            "Custom Gpu Wasskühlblock. Für Gtx1080",
            "Heatkiller IV Wasserkühlung für NVIDIA GTX 1080ti, Acrylglas",
            "NVIDIA GeForce GTX 1080 DreamHack 2016 Launch Plaque Rare Collectible",
            "GPU Halterung für Grafikkarte",
            # "Gehäuse" ist bei einer Karte die Blende mit Lüfter.
            "NVIDIA GeForce GTX 1080 Founder Edition Gehäuse mit Lüfter",
        ],
    )
    def test_zubehoer_ohne_verbindung_wird_erkannt(self, titel):
        assert accessory_reason(titel, vorgabe="GTX 1080") is not None

    def test_eigene_kategorie_hebt_die_regel_auf(self):
        """Eine Gehäuse-Suche darf ihr eigenes Wort nicht verwerfen."""
        titel = "Fractal Design North Gehäuse schwarz"
        assert accessory_reason(titel, vorgabe="Fractal Design") is not None
        assert (
            accessory_reason(
                titel, vorgabe="Fractal Design", skip_nouns=frozenset({"gehaeuse"})
            )
            is None
        )

    @pytest.mark.parametrize(
        "titel",
        [
            # Das Teilwort steckt nur als Bestandteil im Wort - genau so steht
            # es in den Anzeigen, die durchgerutscht sind.
            "Yeston RTX 3050 Ersatzhalterung für Grafikkarte",
            "Ersatzbackplate für RTX 3080 Grafikkarte",
            "PCIe Riserkabel für RTX 3060 Grafikkarte",
            "GPU Kartenhalter für RTX 3060, schwarz",
        ],
    )
    def test_zusammengesetzte_teilwoerter_werden_erkannt(self, titel):
        assert accessory_reason(titel, vorgabe="RTX 3060 Grafikkarte") is not None

    @pytest.mark.parametrize(
        "titel",
        [
            # "mit"/"inkl." macht das zusammengesetzte Wort zur Beigabe.
            "Asus RTX 3060 mit Halterung",
            "MSI RTX 3060 Gaming X mit Backplate",
        ],
    )
    def test_zusammengesetztes_teilwort_als_beigabe(self, titel):
        assert accessory_reason(titel, vorgabe="RTX 3060 Grafikkarte") is None

    def test_suche_nach_dem_teil_hebt_die_regel_auf(self):
        """Wer selbst eine Halterung sucht, verliert sie nicht."""
        titel = "GPU Kartenhalter für Grafikkarte, höhenverstellbar"
        assert accessory_reason(titel, vorgabe="Grafikkarte") is not None
        assert accessory_reason(titel, vorgabe="Halterung") is None

    def test_suche_nach_dem_luefter_hebt_die_regel_auf(self):
        """Wer selbst einen Lüfter sucht, darf Lüfter-Titel nicht verlieren."""
        titel = "Noctua Lüfter für Grafikkarte, 120mm"
        assert accessory_reason(titel, vorgabe="Grafikkarte") is not None
        assert accessory_reason(titel, vorgabe="Lüfter") is None

    def test_vorgabe_text_kommt_aus_der_suche(self):
        text = target_text(_target())
        assert "RTX 3060" in text


class TestProduktabgleich:
    def test_preisliste_verlangt_bekannte_komponente(self):
        liste = _price_list("RTX 3060", "RTX 3080")
        assert (
            title_product_reason("MSI RTX 3060 Gaming X", price_list=liste, category="gpu")
            is None
        )
        assert (
            title_product_reason("GTX 560 Ti Karte", price_list=liste, category="gpu")
            is not None
        )

    def test_fremde_kategorie_wird_abgelehnt(self):
        liste = _price_list("RTX 3060", ("Intel i5-12400F", "cpu"))
        # Die CPU steht in der Liste, passt aber nicht zur gesuchten GPU.
        reason = title_product_reason(
            "Intel i5-12400F Prozessor", price_list=liste, category="gpu"
        )
        assert reason is not None

    def test_ohne_preisliste_zaehlen_die_schlagwoerter(self):
        assert title_product_reason("ASUS RTX 3060 Gaming", term="RTX 3060") is None
        assert title_product_reason("ASUS RTX 3070 Gaming", term="RTX 3060") is not None

    def test_leerer_titel_wird_abgelehnt(self):
        assert title_product_reason("   ", term="RTX 3060") is not None


class TestFilter:
    def test_urteil_wird_vergeben(self):
        filt = filter_factory()
        verdicts = filt.review(
            _target(), [_Angebot("MSI RTX 3060 Gaming X"), _Angebot("GPU Halterung für RTX 3060")]
        )
        assert [v.passt for v in verdicts] == [True, False]
        assert verdicts[1].quelle == "regel"
        assert filt.stats.passt == 1 and filt.stats.passt_nicht == 1
        assert filt.stats.zubehoer >= 1
        assert filt.stats.geprueft == 2
        assert filt.stats.aktiv is True

    def test_jeder_titel_bekommt_ein_urteil(self):
        """Es gibt keinen Titel ohne Urteil mehr - auch nicht bei Zubehör."""
        filt = filter_factory()
        verdicts = filt.review(_target(), [_Angebot("GPU Halterung für RTX 3060")])
        assert verdicts[0] is not None and verdicts[0].passt is False

    def test_jeder_titel_wird_bei_jedem_lauf_neu_geprueft(self):
        """Kein Bestandsschutz: Auch ein gespeicherter Titel wird erneut beurteilt.

        Sonst bliebe alles, was eine frühere, schwächere Fassung der Regeln
        durchgelassen hat, für immer in der Ergebnisliste stehen.
        """
        filt = filter_factory()
        verdicts = filt.review(
            _target(),
            [_Angebot("MSI RTX 3060 Gaming X"), _Angebot("GPU Halterung für RTX 3060")],
        )
        assert verdicts[0].passt is True
        assert verdicts[1].passt is False
        assert not hasattr(filt, "known")

    def test_lokaler_import_wird_nicht_gefiltert(self):
        filt = filter_factory()
        assert filt.active_for("local_import") is False
        assert filt.active_for("ebay_web") is True

    def test_komplette_geraete_werden_erkannt(self):
        """Ein PC oder Notebook enthält die Karte - verkauft wird das Gerät."""
        for titel in [
            "Gaming PC | RTX 5090 | Ultra 9 275HX | 1TB | 32GB DDR5",
            "Gaming Laptop | RTX 5090 | Ultra 9 275HX | 32GB DDR5 | 2TB",
            "Lenovo Legion Pro 7 16IAX10H (Gen 10, CTO) – RTX 5090",
            "Razer Blade 16 AI 9 HX 370 RTX 5090 32GB 2TB 240 Hz OLED Gaming",
            "HP OMEN 17 | RTX 4090 16GB | i7-13700HX | 32GB | 1TB | QHD 240Hz",
            "GIGABYTE AORUS Master 16, Ultra 9, 32GB, 2TB, RTX 5090, Neu",
        ]:
            assert accessory_reason(titel, vorgabe="RTX 5090") is not None, titel

    def test_modell_ausdruecklich_nicht_dabei(self):
        """"Ohne RTX 5090" verkauft den Rechner ohne die Karte."""
        assert (
            accessory_reason(
                "HP OMEN 45L – i9 285k – Ohne RTX 5090 – 16 GB DDR5 – 500 GB",
                vorgabe="RTX 5090",
            )
            is not None
        )

    def test_beigaben_bleiben_erlaubt(self):
        """"mit/in/inkl. OVP" beschreibt eine echte Karte, nicht die Verpackung."""
        for titel in [
            "Gigabyte RTX 3060 Windforce, OVP und Rechnung dabei",
            "ASUS GeForce RTX 3070 DUAL OC 8GB Grafikkarte mit OVP",
            "MSI NVIDIA GeForce RTX 3070 Ti Ventus 3X OC 8GB inklusive OVP/Originalverpackung",
            "KFA2 RTX 3070 Ti SG 8GB – inkl. OVP & Zubehör nur Abholung",
            "MSI AMD Radeon Videokarte RX 580 ARMOR 8G OC ohne OVP",
        ]:
            assert accessory_reason(titel, vorgabe="RTX 3070 Ti") is None, titel

    def test_varianten_desselben_modells_sind_keine_sammelanzeige(self):
        """"RTX 3080 Ti 12GB" nennt ein Modell - nicht drei Listeneinträge.

        Die Liste führt für die 3080 drei Einträge ("RTX 3080 10GB", "RTX 3080
        12GB", "RTX 3080 Ti"). Echte Karten mit Variante im Titel fielen
        dadurch als Sammelanzeige heraus - beobachtet bei RTX 3080 Ti, RTX 4060
        Ti und RTX 5060 Ti.
        """
        liste = _price_list("RTX 3080 10GB", "RTX 3080 12GB", "RTX 3080 Ti")
        for titel in [
            "MSI RTX 3080 Ti SUPRIM X 12GB GDDR6X Grafikkarte (OVP)",
            "ASUS ROG Strix LC RTX 3080 Ti OC 12G ROG Strix OC 12 GB GDDR6X Grafikkarte",
        ]:
            assert (
                title_product_reason(
                    titel, term="RTX 3080 Ti", price_list=liste, category="gpu"
                )
                is None
            ), titel

    def test_sammelanzeige_vieler_modelle_bleibt_erkannt(self):
        liste = _price_list("RTX 3080 10GB", "RTX 3080 12GB", "RTX 3080 Ti", "GTX 1080")
        ergebnis = title_product_reason(
            "Grafikkarten: RTX 3080Ti 2060 GTX1080 1060 RX580 uvm",
            term="RTX 3080 Ti",
            price_list=liste,
            category="gpu",
        )
        assert ergebnis is not None
        assert ergebnis[0] == ART_SAMMELANZEIGE

    def test_zusammenfassung_nennt_die_zahlen(self):
        filt = filter_factory()
        filt.review(_target(), [_Angebot("MSI RTX 3060 Gaming X")])
        text = filt.summary()
        assert "1" in text and "Titelprüfung" in text


class TestBeschreibung:
    """Der Beschreibungstext rettet nur eine fehlende Produktzuordnung.

    Genau der Fall aus der Praxis: Kleinanzeigen zeigt unter dem Titel oft nur
    "Grafikkarte" und nennt das Modell im Text.
    """

    TITEL_OHNE_MODELL = "Grafikkarte abzugeben, siehe Beschreibung"
    TEXT_MIT_MODELL = "Verkaufe eine MSI RTX 3060 Gaming X, 12GB, wenig genutzt."

    def test_fehlendes_modell_wird_gerettet(self):
        ergebnis, gerettet = match_product(
            self.TITEL_OHNE_MODELL, self.TEXT_MIT_MODELL, term="RTX 3060"
        )
        assert ergebnis is None
        assert gerettet is True

    def test_ohne_beschreibung_bleibt_es_beim_urteil(self):
        ergebnis, gerettet = match_product(self.TITEL_OHNE_MODELL, None, term="RTX 3060")
        assert ergebnis is not None
        assert gerettet is False

    def test_beschreibung_ohne_modell_aendert_nichts(self):
        ergebnis, gerettet = match_product(
            self.TITEL_OHNE_MODELL, "Nur Abholung, keine Garantie, wenig genutzt.", term="RTX 3060"
        )
        assert ergebnis is not None
        assert gerettet is False

    def test_teil_titel_wird_nicht_gerettet(self):
        """Verkauft der Titel ein Teil, hebt die Beschreibung das nicht auf."""
        ergebnis, gerettet = match_product(
            "GPU Halterung für RTX 3060, Stütze", self.TEXT_MIT_MODELL, term="RTX 3060"
        )
        assert ergebnis is not None
        assert ergebnis[0] == ART_TEIL
        assert gerettet is False

    def test_verpackungstitel_wird_nicht_gerettet(self):
        ergebnis, gerettet = match_product(
            "NUR OVP MSI RTX 3060", self.TEXT_MIT_MODELL, term="RTX 3060"
        )
        assert ergebnis is not None
        assert gerettet is False

    def test_beschreibung_muss_die_liste_erfuellen(self):
        liste = _price_list("RTX 3060")
        ergebnis, gerettet = match_product(
            "Grafikkarte abzugeben",
            "MSI RTX 3060 Gaming X, 12GB",
            term="RTX 3060",
            price_list=liste,
            category="gpu",
        )
        assert ergebnis is None and gerettet is True
        fremd, fremd_gerettet = match_product(
            "Grafikkarte abzugeben",
            "MSI RTX 3070 Gaming X, 8GB",
            term="RTX 3060",
            price_list=liste,
            category="gpu",
        )
        assert fremd is not None and fremd_gerettet is False

    def test_filter_zaehlt_gerettete_getrennt(self):
        filt = filter_factory()
        verdicts = filt.review(
            _target(),
            [
                _Angebot(self.TITEL_OHNE_MODELL, self.TEXT_MIT_MODELL),
                _Angebot("GPU Halterung für RTX 3060", self.TEXT_MIT_MODELL),
            ],
        )
        assert [v.passt for v in verdicts] == [True, False]
        assert verdicts[0].aus_beschreibung is True
        assert verdicts[1].aus_beschreibung is False
        assert filt.stats.passt == 1
        assert filt.stats.aus_beschreibung == 1
        assert filt.stats.to_dict()["aus_beschreibung"] == 1
        assert "Beschreibung" in filt.summary()
