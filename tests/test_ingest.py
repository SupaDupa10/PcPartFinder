"""Tests fuer die Ingestion: von Roh-Angebot zu gespeichertem Angebot.

Deckt die Pflichtfaelle aus Abschnitt 17 ab: Auktionen, Duplikate,
Preisaenderungen, fehlende Versandkosten, unbekanntes Datum, unbekannter
Zustand und fehlerhafte Einzelangebote.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend.connectors.base import RawListing
from backend.domain.dedup import canonical_url
from backend.domain.hardware import HardwareResolver
from backend.domain.price_limits import PriceLimit, PriceList
from backend.knowledge.base import KnowledgeBase
from backend.models import STATUS_GONE, Listing
from backend.search.ingest import (
    RESULT_AUCTION,
    RESULT_DEFECT,
    RESULT_ERROR,
    RESULT_EXCLUSION_TERM,
    RESULT_LOCATION,
    RESULT_NEW_ITEM,
    RESULT_PRICE,
    RESULT_RELEVANCE,
    RESULT_TITLE_FILTER,
    RESULT_UNKNOWN_FORMAT,
    RESULT_USABLE,
    IngestOptions,
    OfferIngestor,
    defect_or_new_reason,
)
from backend.services.repository import (
    OUTCOME_NEW,
    OUTCOME_UNCHANGED,
    OUTCOME_UPDATED,
    upsert_listing,
)
from backend.services.repository import build_dedup_fields, price_history_for

NOW = datetime.now(timezone.utc)


@pytest.fixture()
def ingestor(db_session, knowledge):
    def factory(**options) -> OfferIngestor:
        return OfferIngestor(
            db_session,
            knowledge=knowledge,
            resolver=HardwareResolver(knowledge),
            options=IngestOptions(**options),
        )

    return factory


def raw(**overrides) -> RawListing:
    payload = {
        "platform": "test",
        "platform_listing_id": "abc-1",
        "url": "https://example.invalid/angebot/rtx-3080?utm_source=x",
        "title": "ASUS RTX 3080 TUF Gaming 10GB Grafikkarte",
        "description": "Karte funktioniert einwandfrei, mit Rechnung und OVP.",
        "price": 210.0,
        "shipping_cost": 5.99,
        "shipping_cost_known": True,
        "buying_options": ["FIXED_PRICE"],
        "condition_text": "Gebraucht, sehr gut",
        "published_at": NOW - timedelta(days=5),
        "seller_name": "techsammler",
        "postal_code": "80331",
        "location_text": "Muenchen",
    }
    payload.update(overrides)
    return RawListing(**payload)


class TestFormatRules:
    def test_festpreisangebot_wird_gespeichert(self, ingestor, db_session):
        outcome = ingestor().ingest(raw())
        assert outcome.result == RESULT_USABLE
        assert outcome.outcome == OUTCOME_NEW
        assert outcome.listing_id is not None

    def test_auktion_wird_ausgeschlossen(self, ingestor):
        outcome = ingestor().ingest(
            raw(buying_options=["AUCTION"], current_bid=150.0, bid_count=7)
        )
        assert outcome.result == RESULT_AUCTION
        assert outcome.stored is False
        assert outcome.reason

    def test_unbekanntes_format_wird_ausgeschlossen(self, ingestor):
        outcome = ingestor().ingest(raw(buying_options=None))
        assert outcome.result == RESULT_UNKNOWN_FORMAT
        assert outcome.stored is False

    def test_gemischtes_format_mit_sofortkauf_ist_zulaessig(self, ingestor):
        outcome = ingestor().ingest(
            raw(buying_options=["AUCTION", "FIXED_PRICE"], current_bid=150.0, price=280.0)
        )
        assert outcome.result == RESULT_USABLE
        assert any("Sofort-Kauf" in warning for warning in outcome.warnings)

    def test_auktionen_koennen_bei_bedarf_zugelassen_werden(self, ingestor):
        outcome = ingestor(exclude_auctions=False).ingest(
            raw(buying_options=["AUCTION"], current_bid=150.0)
        )
        assert outcome.result == RESULT_USABLE


class TestPriceRules:
    def test_preis_ueber_dem_limit_wird_abgelehnt(self, ingestor):
        outcome = ingestor(max_price=200.0).ingest(raw(price=210.0))
        assert outcome.result == RESULT_PRICE
        assert "Limit" in (outcome.reason or "")

    def test_gesamtpreis_zaehlt_fuer_das_limit(self, ingestor):
        """Artikelpreis im Limit, aber mit Versand darueber."""
        outcome = ingestor(max_price=212.0).ingest(raw(price=210.0, shipping_cost=5.99))
        assert outcome.result == RESULT_PRICE

    def test_unbekannte_versandkosten_werden_nicht_als_null_gerechnet(self, ingestor, db_session):
        outcome = ingestor(max_price=211.0).ingest(
            raw(price=210.0, shipping_cost=None, shipping_cost_known=False)
        )
        assert outcome.result == RESULT_USABLE
        listing = db_session.get(Listing, outcome.listing_id)
        assert listing.total_price == 210.0
        assert listing.shipping_cost_known is False
        assert any("Versandkosten nicht angegeben" in w for w in outcome.warnings)

    def test_angebot_ausserhalb_des_zeitraums(self, ingestor):
        old = NOW - timedelta(days=400)
        outcome = ingestor(days_back=150).ingest(raw(published_at=old))
        assert outcome.result == RESULT_PRICE
        assert "Zeitraum" in (outcome.reason or "")

    def test_preis_ohne_wert_wird_abgelehnt(self, ingestor):
        outcome = ingestor(max_price=300.0).ingest(raw(price=None))
        assert outcome.result == RESULT_PRICE


class TestMindestpreis:
    """Der Mindestpreis eines Produkts ist ein Viertel seines Höchstpreises.

    Sehr günstige Angebote sind im Gebrauchtmarkt Zubehör, Einzelteile oder
    defekte Ware. Sie sind kein Treffer zum gesuchten Produkt und werden
    deshalb nicht gespeichert.
    """

    def test_angebot_unter_dem_mindestpreis_wird_abgelehnt(self, ingestor):
        # Höchstpreis 400 € -> Mindestpreis 100 €.
        outcome = ingestor(max_price=400.0).ingest(
            raw(price=90.0, shipping_cost=0.0, shipping_cost_known=True)
        )
        assert outcome.result == RESULT_PRICE
        assert outcome.stored is False
        assert "Mindestpreis" in (outcome.reason or "")

    def test_genau_am_mindestpreis_ist_erlaubt(self, ingestor):
        outcome = ingestor(max_price=400.0).ingest(
            raw(price=100.0, shipping_cost=0.0, shipping_cost_known=True)
        )
        assert outcome.result == RESULT_USABLE

    def test_gesamtpreis_zaehlt_fuer_den_mindestpreis(self, ingestor):
        outcome = ingestor(max_price=400.0).ingest(
            raw(price=95.0, shipping_cost=0.0, shipping_cost_known=True)
        )
        assert outcome.result == RESULT_PRICE

    def test_unbekannte_versandkosten_zaehlen_wie_der_artikelpreis(self, ingestor):
        outcome = ingestor(max_price=400.0).ingest(
            raw(price=120.0, shipping_cost=None, shipping_cost_known=False)
        )
        assert outcome.result == RESULT_USABLE

    def test_je_anfrage_gilt_ihr_eigener_mindestpreis(self, ingestor):
        """Im Preislisten-Modus hat jede Komponente ihr eigenes Limit."""
        outcome = ingestor().ingest(
            raw(price=90.0, shipping_cost=0.0, shipping_cost_known=True), min_price=100.0
        )
        assert outcome.result == RESULT_PRICE
        assert "Mindestpreis" in (outcome.reason or "")

    def test_ohne_hoechstpreis_gibt_es_keinen_mindestpreis(self, ingestor):
        outcome = ingestor().ingest(raw(price=1.0))
        assert outcome.result == RESULT_USABLE

    def test_vorpruefung_erkennt_den_mindestpreis(self, ingestor):
        """Die Vorprüfung sortiert zu günstige Angebote vor der KI aus."""
        instance = ingestor(max_price=400.0)
        offer = raw(price=90.0, shipping_cost=0.0, shipping_cost_known=True)
        vorab = instance.precheck(offer)
        assert vorab is not None
        assert vorab.result == RESULT_PRICE
        assert instance.ingest(offer).result == RESULT_PRICE


class TestPriceListLimits:
    """Kauf-Limits aus der Liste: über dem Limit wird nichts gespeichert."""

    @staticmethod
    def _list(max_price: float, term: str = "RTX 3080 10GB") -> PriceList:
        return PriceList(
            entries=[
                PriceLimit(
                    group="gpu",
                    group_label="Grafikkarten",
                    category="gpu",
                    name=f"NVIDIA GeForce {term}",
                    term=term,
                    max_price=max_price,
                )
            ]
        )

    def test_angebot_ueber_dem_kauf_limit_wird_verworfen(self, ingestor):
        """Artikelpreis im Limit, Gesamtpreis darüber: kein Schnäppchen mehr."""
        outcome = ingestor(price_list=self._list(215.0)).ingest(
            raw(price=210.0, shipping_cost=5.99)
        )
        assert outcome.result == RESULT_PRICE
        assert outcome.stored is False
        assert "Kauf-Limit" in (outcome.reason or "")

    def test_angebot_unter_dem_kauf_limit_wird_gespeichert(self, ingestor, db_session):
        outcome = ingestor(price_list=self._list(240.0)).ingest(
            raw(price=210.0, shipping_cost=5.99)
        )
        assert outcome.result == RESULT_USABLE
        listing = db_session.get(Listing, outcome.listing_id)
        assert listing.total_price == pytest.approx(215.99)

    def test_genau_am_limit_ist_erlaubt(self, ingestor):
        outcome = ingestor(price_list=self._list(215.99)).ingest(
            raw(price=210.0, shipping_cost=5.99)
        )
        assert outcome.result == RESULT_USABLE

    def test_komponente_ohne_eintrag_bleibt_erlaubt(self, ingestor):
        outcome = ingestor(price_list=self._list(100.0, term="RX 6600")).ingest(raw())
        assert outcome.result == RESULT_USABLE

    def test_ohne_liste_gibt_es_keine_grenze(self, ingestor):
        outcome = ingestor().ingest(raw(price=900.0))
        assert outcome.result == RESULT_USABLE


class TestMissingData:
    def test_unbekanntes_veroeffentlichungsdatum(self, ingestor, db_session):
        outcome = ingestor(days_back=150).ingest(raw(published_at=None))
        assert outcome.result == RESULT_USABLE
        listing = db_session.get(Listing, outcome.listing_id)
        assert listing.published_at is None
        assert listing.published_at_known is False
        assert any("Veroeffentlichungszeitpunkt unbekannt" in w or "Veröffentlichungszeitpunkt unbekannt" in w for w in outcome.warnings)

    def test_unbekannter_zustand(self, ingestor, db_session):
        outcome = ingestor().ingest(raw(condition_text=None, condition_id=None))
        assert outcome.result == RESULT_USABLE
        listing = db_session.get(Listing, outcome.listing_id)
        assert listing.condition_rank is None

    def test_zustand_aus_dem_kleinanzeigen_text_wird_gespeichert(
        self, ingestor, db_session
    ):
        """Die Uebergabe vom Connector an die Ingestion muss tragen.

        Kleinanzeigen nennt keinen Zustand; der Connector leitet ihn aus dem
        Anzeigetext ab. Kommt die Bezeichnung hier nicht als Rang an, bliebe die
        Ableitung ohne Wirkung auf die Bewertung.
        """
        from backend.connectors.kleinanzeigen_web import _derive_condition

        zustand = _derive_condition("ASUS RTX 3080 Grafikkarte, wie neu, wenig genutzt")
        assert zustand is not None
        outcome = ingestor().ingest(raw(condition_text=zustand))
        assert outcome.result == RESULT_USABLE
        listing = db_session.get(Listing, outcome.listing_id)
        assert listing.condition_rank == 3

    def test_gespeichertes_angebot_ohne_zustand_wird_nachgezogen(
        self, ingestor, db_session
    ):
        """Ein schon gespeichertes Angebot erhaelt den Zustand beim naechsten
        Sehen - sonst bliebe die Ableitung fuer den Bestand wirkungslos."""
        from backend.connectors.kleinanzeigen_web import _derive_condition

        zustand = _derive_condition("ASUS RTX 3080 Grafikkarte, wie neu")
        assert zustand is not None
        kennung = "ka-nachzug-1"
        erst = ingestor().ingest(
            raw(platform_listing_id=kennung, condition_text=None, condition_id=None)
        )
        assert erst.outcome == OUTCOME_NEW
        assert db_session.get(Listing, erst.listing_id).condition_rank is None

        nachher = ingestor().ingest(
            raw(platform_listing_id=kennung, condition_text=zustand)
        )
        assert nachher.listing_id == erst.listing_id
        assert nachher.outcome == OUTCOME_UPDATED
        assert db_session.get(Listing, nachher.listing_id).condition_rank == 3

    def test_neuware_hinweis_im_freitext_loescht_nichts(self, ingestor, db_session):
        """Wird der Zustand nicht abgeleitet, darf die Ingestion nicht auf "Neu"
        schliessen - sonst wuerde ein Freitext ein echtes Angebot loeschen."""
        outcome = ingestor().ingest(
            raw(
                condition_text=None,
                condition_id=None,
                title="MSI GeForce GTX 1660 super Grafikkarte",
                description="Sie ist bereit fuer den Einsatz in einem neuen System.",
            )
        )
        assert outcome.result == RESULT_USABLE
        assert db_session.get(Listing, outcome.listing_id).condition_rank is None

    def test_unbekanntes_modell_verhindert_das_speichern_nicht(self, ingestor, db_session):
        outcome = ingestor().ingest(raw(title="Grafikkarte, laeuft", description=None))
        assert outcome.result == RESULT_USABLE
        listing = db_session.get(Listing, outcome.listing_id)
        assert listing.model is None
        assert outcome.warnings


class TestExclusions:
    def test_ausschlussbegriff_der_anfrage(self, ingestor):
        outcome = ingestor().ingest(
            raw(title="RTX 3080 Aufkleber Sammlung"), exclusion_terms=["aufkleber"]
        )
        assert outcome.result == RESULT_EXCLUSION_TERM

    def test_anfragebezogene_ausschluesse_gelten_nur_fuer_diesen_aufruf(self, ingestor):
        """Ein Aufruf darf die Instanz nicht dauerhaft veraendern."""
        subject = ingestor()
        assert subject.ingest(raw(title="RTX 3080 Aufkleber"), exclusion_terms=["aufkleber"]).result == RESULT_EXCLUSION_TERM
        assert subject.ingest(raw()).result == RESULT_USABLE

    def test_ausschlussbegriff_der_konfiguration(self, ingestor):
        outcome = ingestor(exclusion_terms=["sticker"]).ingest(
            raw(title="RTX 3080 Aufkleber Sticker Sammlung")
        )
        assert outcome.result == RESULT_EXCLUSION_TERM

    def test_problematisches_modell_aus_der_wissensbasis(self, db_session, tmp_path):
        path = tmp_path / "kb.yaml"
        path.write_text(
            "entries:\n  - category: gpu\n    model: RTX 3080\n    problematic: true\n",
            encoding="utf-8",
        )
        base = KnowledgeBase()
        base.load([path])
        ingestor = OfferIngestor(
            db_session,
            knowledge=base,
            resolver=HardwareResolver(base),
            options=IngestOptions(exclude_problematic=True),
        )
        outcome = ingestor.ingest(raw())
        assert outcome.result == RESULT_EXCLUSION_TERM
        assert "problematisch" in (outcome.reason or "")


class TestLocation:
    def test_abholung_ohne_standort_ist_nicht_verwertbar(self, ingestor):
        outcome = ingestor(delivery="pickup").ingest(raw(postal_code=None))
        assert outcome.result == RESULT_LOCATION

    def test_zu_weite_entfernung_wird_abgelehnt(self, ingestor):
        outcome = ingestor(delivery="pickup", postal_code="10115", radius_km=50).ingest(
            raw(postal_code="80331")
        )
        assert outcome.result == RESULT_LOCATION
        assert "Entfernung" in (outcome.reason or "")

    def test_versand_filtert_reine_abholangebote(self, ingestor):
        outcome = ingestor(delivery="shipping").ingest(
            raw(pickup_available=True, shipping_cost=None, shipping_cost_known=False)
        )
        assert outcome.result == RESULT_LOCATION

    def test_beide_wege_erzeugen_entfernungshinweis(self, ingestor):
        outcome = ingestor(delivery="both", postal_code="10115").ingest(raw())
        assert outcome.result == RESULT_USABLE
        assert any(
            "Abholung grob" in w and "km entfernt" in w for w in outcome.warnings
        )


class TestProduktRelevanz:
    """Suche nach konkretem Produktnamen: nur passende Angebote speichern."""

    def test_unpassendes_angebot_wird_verworfen(self, ingestor):
        outcome = ingestor(
            required_tokens=["rtx", "3080", "ti"], required_min_matches=2
        ).ingest(
            raw(
                title="MSI RTX 3070 Gaming X Trio Grafikkarte",
                description=None,
            )
        )
        assert outcome.result == RESULT_RELEVANCE
        assert outcome.stored is False
        assert "Produkt" in (outcome.reason or "")

    def test_passendes_angebot_bleibt(self, ingestor):
        outcome = ingestor(
            required_tokens=["rtx", "3080", "ti"], required_min_matches=2
        ).ingest(raw(title="ASUS RTX 3080 Ti TUF Gaming Grafikkarte", description=None))
        assert outcome.result == RESULT_USABLE

    def test_zusammengeschriebene_schreibweise_zaehlt(self, ingestor):
        outcome = ingestor(
            required_tokens=["rtx", "3080"], required_min_matches=2
        ).ingest(raw(title="RTX3080 Founders Edition", description=None))
        assert outcome.result == RESULT_USABLE

    def test_ohne_produktfilter_bleibt_alles_wie_bisher(self, ingestor):
        outcome = ingestor().ingest(raw())
        assert outcome.result == RESULT_USABLE

    def test_breite_begriffe_allein_reichen_nicht(self, ingestor):
        outcome = ingestor(
            required_tokens=["samsung", "990", "pro", "2tb"], required_min_matches=2
        ).ingest(raw(title="Samsung SSD 1TB SATA", description=None))
        assert outcome.result == RESULT_RELEVANCE


class TestVorpruefung:
    """Die Vorprüfung prüft dieselben Schritte wie die Ingestion (1, 3-5) -
    und zusätzlich die eindeutigen Verpackungs- und Papierangebote. Sie darf
    dabei nichts wegnehmen, was die Ingestion angenommen hätte."""

    @pytest.mark.parametrize(
        "overrides, options",
        [
            ({}, {}),
            (
                {"title": "RTX 3080 Grafikkarte defekt als Ersatzteil"},
                {"exclusion_terms": ["defekt"]},
            ),
            ({"buying_options": ["AUCTION"], "platform_format": "AUCTION"}, {}),
            ({"price": 900.0}, {"max_price": 300.0}),
            ({"buying_options": [], "platform_format": None}, {}),
            (
                {"postal_code": "99998", "location_text": "Weit weg"},
                {"delivery": "pickup", "postal_code": "10115", "radius_km": 20.0},
            ),
            (
                {"published_at": NOW - timedelta(days=400)},
                {"days_back": 30},
            ),
        ],
    )
    def test_gleiches_ergebnis_wie_die_ingestion(self, ingestor, overrides, options):
        instance = ingestor(**options)
        offer = raw(**overrides)
        vorab = instance.precheck(offer)
        final = instance.ingest(offer)
        if vorab is None:
            assert final.result == RESULT_USABLE
        else:
            assert vorab.result == final.result
            assert final.stored is False

    def test_produktabgleich_laeuft_nur_bei_der_ingestion(self, ingestor):
        """Die Titelprüfung ersetzt den Produktabgleich - die Vorprüfung lässt
        ihn bewusst aus, die Ingestion nicht."""
        instance = ingestor(required_tokens=["rtx", "3080", "ti"], required_min_matches=2)
        offer = raw(title="MSI RTX 3070 Gaming X Trio Grafikkarte", description=None)
        assert instance.precheck(offer) is None
        assert instance.ingest(offer).result == RESULT_RELEVANCE

    @pytest.mark.parametrize(
        "titel",
        [
            "Original OVP Verpackung RTX 3060 Gaming X Trio - nur OVP",
            "RTX 3060 Karton ohne Karte, nur der Karton",
            "Leere Verpackung MSI RTX 3060 Gaming X",
            "RTX 3060 nur Rechnung und Anleitung",
            "Nur Anleitung RTX 3060 Gaming X Trio",
        ],
    )
    def test_nur_verpackung_wird_vorab_erkannt(self, ingestor, titel):
        """Ein Titel, der nur Verpackung oder Papiere anbietet, ist kein Angebot.

        Das erkennt schon die Vorprüfung - also auch dann, wenn die
        Titelprüfung gerade nicht greift.
        """
        instance = ingestor()
        offer = raw(title=titel, description=None)
        vorab = instance.precheck(offer)
        assert vorab is not None
        assert vorab.result == RESULT_TITLE_FILTER
        assert "Verpackung" in (vorab.reason or "")

    @pytest.mark.parametrize(
        "titel",
        [
            "MSI RTX 3060 Gaming X Trio - mit OVP und Rechnung",
            "Asus RTX 3060 Dual 12GB OC, OVP vorhanden",
            "RTX 3060 12GB Grafikkarte, Rechnung dabei, OVP komplett",
        ],
    )
    def test_mit_ovp_und_rechnung_bleibt_gueltig(self, ingestor, titel):
        """Verpackung und Rechnung als Eigenschaft sind kein Ausschlussgrund."""
        instance = ingestor()
        offer = raw(title=titel, description=None)
        assert instance.precheck(offer) is None
        assert instance.ingest(offer).result == RESULT_USABLE

    @pytest.mark.parametrize(
        "titel",
        [
            "NUR KÜHLER!! MSI RTX 3060 GAMING X 12G",
            "Nur Lüfter - MSI RTX 3060 Gaming X Trio",
            "nur der Karton MSI RTX 3060 Gaming X",
            "Nur die Backplate RTX 3060 Gaming X",
            "NUR WASSERBLOCK RTX 3060",
            "nur handbuch rtx 3060",
        ],
    )
    def test_nur_ein_teil_wird_vorab_erkannt(self, ingestor, titel):
        """Ein Titel, der mit "nur <Teil>" beginnt, verkauft dieses Teil - nicht das
        Produkt. Solche Angebote müssen auch dann durchfallen, wenn die
        Titelprüfung gerade nicht greift.
        """
        instance = ingestor()
        offer = raw(title=titel, description=None)
        vorab = instance.precheck(offer)
        assert vorab is not None
        assert vorab.result == RESULT_TITLE_FILTER

    @pytest.mark.parametrize(
        "titel",
        [
            "Grafikkarte RTX 3060, nur Kühler getauscht",
            "MSI RTX 3060 Gaming X - Lüfter und Kühler neu",
            "RTX 3060 12GB mit Wasserblock und Kühler",
            "Grafikkarte RTX 3060 mit OVP und Rechnung",
        ],
    )
    def test_teilworte_mitten_im_titel_schliessen_nicht_aus(self, ingestor, titel):
        """Teile als Eigenschaft des Angebots sind kein Ausschlussgrund."""
        instance = ingestor()
        offer = raw(title=titel, description=None)
        assert instance.precheck(offer) is None

    def test_verpackungspruefung_greift_nur_in_der_vorpruefung(self, ingestor):
        """Die Ingestion selbst prüft Titel weiterhin nicht (dort greift die Titelprüfung)."""
        instance = ingestor()
        offer = raw(title="Nur OVP Karton RTX 3060 ohne Karte", description=None)
        assert instance.precheck(offer).result == RESULT_TITLE_FILTER
        assert instance.ingest(offer).result == RESULT_USABLE

    def test_vorpruefung_speichert_nichts(self, ingestor, db_session):
        instance = ingestor()
        assert instance.precheck(raw()) is None
        assert db_session.query(Listing).count() == 0


class TestDeduplication:
    def test_gleiche_plattform_id_erzeugt_kein_duplikat(self, ingestor, db_session):
        ingestor().ingest(raw())
        ingestor().ingest(raw(url="https://example.invalid/andere-url"))
        assert db_session.query(Listing).count() == 1

    def test_gleiche_url_erzeugt_kein_duplikat(self, ingestor, db_session):
        ingestor().ingest(raw(platform_listing_id=None))
        ingestor().ingest(raw(platform_listing_id=None, title="Anderer Titel"))
        assert db_session.query(Listing).count() == 1

    def test_tracking_parameter_aendern_die_identitaet_nicht(self, ingestor, db_session):
        ingestor().ingest(raw(url="https://example.invalid/a/1?utm_source=x"))
        ingestor().ingest(raw(platform_listing_id=None, url="https://example.invalid/a/1#frag"))
        assert db_session.query(Listing).count() == 1

    def test_anderes_angebot_wird_getrennt_gespeichert(self, ingestor, db_session):
        """Unterschiedliche Plattform-IDs sind unterschiedliche Anzeigen,
        auch wenn Titel, Preis und Verkaeufer identisch sind."""
        ingestor().ingest(raw())
        ingestor().ingest(raw(platform_listing_id="abc-2", url="https://example.invalid/2"))
        assert db_session.query(Listing).count() == 2

    def test_gleiche_plattform_id_bleibt_eindeutig(self, ingestor, db_session):
        ingestor().ingest(raw())
        ingestor().ingest(raw(url="https://example.invalid/voellig-anders"))
        assert db_session.query(Listing).count() == 1

    def test_doppelte_angebote_im_datenbestand_werden_gezaehlt(self, ingestor):
        first = ingestor().ingest(raw())
        second = ingestor().ingest(raw())
        assert first.outcome == OUTCOME_NEW
        assert second.outcome == OUTCOME_UNCHANGED
        assert second.listing_id == first.listing_id

    def test_inhaltliche_duplikate_ohne_id_und_unterschiedlicher_url(self, db_session, knowledge):
        """Fallback-Stufe: gleicher Titel, Preis und Verkaeufer."""
        ingestor = OfferIngestor(
            db_session,
            knowledge=knowledge,
            resolver=HardwareResolver(knowledge),
            options=IngestOptions(),
        )
        ingestor.ingest(raw(platform_listing_id=None, url="https://example.invalid/1"))
        ingestor.ingest(raw(platform_listing_id=None, url="https://example.invalid/2"))
        assert db_session.query(Listing).count() == 1


class TestPriceChanges:
    def test_preisaenderung_wird_in_der_historie_festgehalten(self, ingestor, db_session):
        first = ingestor().ingest(raw(price=210.0))
        ingestor().ingest(raw(price=180.0))

        entry = db_session.get(Listing, first.listing_id)
        assert entry.price == 180.0
        history = price_history_for(db_session, entry.id)
        assert [point.price for point in history] == [210.0, 180.0]

    def test_unveraenderter_preis_erzeugt_keinen_history_eintrag(self, ingestor, db_session):
        first = ingestor().ingest(raw())
        ingestor().ingest(raw())
        assert len(price_history_for(db_session, first.listing_id)) == 1

    def test_wechselnde_verfuegbarkeit(self, ingestor, db_session):
        first = ingestor().ingest(raw())
        listing = db_session.get(Listing, first.listing_id)
        from backend.services.repository import mark_status

        mark_status(db_session, listing, STATUS_GONE, note="Bei erneutem Abruf nicht mehr gefunden")
        assert listing.status == STATUS_GONE
        assert any("nicht mehr gefunden" in w for w in listing.warnings)


class TestErrorIsolation:
    def test_aussortierte_angebote_stoppen_den_lauf_nicht(self, ingestor):
        """Ein aussortiertes Angebot darf die Verarbeitung des naechsten nicht verhindern."""
        odd = RawListing(platform="test", url="https://example.invalid/1", title="Kaputt")
        assert ingestor().ingest(odd).result == RESULT_UNKNOWN_FORMAT
        assert ingestor().ingest(raw()).result == RESULT_USABLE

    def test_leerer_titel_bricht_nichts_ab(self, ingestor):
        outcome = ingestor().ingest(raw(title="", description=None))
        # Entweder sauber gespeichert oder kontrolliert aussortiert - nie ein Absturz.
        assert outcome.result in {RESULT_USABLE, RESULT_ERROR, RESULT_PRICE}
        assert ingestor().ingest(raw()).result == RESULT_USABLE

    def test_ingest_faengt_ausnahmen_ab(self, db_session, knowledge, monkeypatch):
        ingestor = OfferIngestor(
            db_session,
            knowledge=knowledge,
            resolver=HardwareResolver(knowledge),
            options=IngestOptions(),
        )

        def boom(*_args, **_kwargs):
            raise RuntimeError("kaputte Daten")

        monkeypatch.setattr(ingestor, "_within_budget", boom)
        outcome = ingestor.ingest(raw())
        assert outcome.result == RESULT_ERROR
        assert "RuntimeError" in (outcome.reason or "")

    def test_zehn_angebote_mit_einem_fehler(self, db_session, knowledge, monkeypatch):
        ingestor = OfferIngestor(
            db_session,
            knowledge=knowledge,
            resolver=HardwareResolver(knowledge),
            options=IngestOptions(),
        )
        original = ingestor._within_budget
        calls = {"count": 0}

        def flaky(*args, **kwargs):
            calls["count"] += 1
            if calls["count"] == 3:
                raise ValueError("nur dieses eine Angebot ist kaputt")
            return original(*args, **kwargs)

        monkeypatch.setattr(ingestor, "_within_budget", flaky)
        results = [
            ingestor.ingest(raw(platform_listing_id=f"id-{i}", url=f"https://example.invalid/{i}"))
            for i in range(10)
        ]
        assert sum(1 for r in results if r.result == RESULT_ERROR) == 1
        assert sum(1 for r in results if r.result == RESULT_USABLE) == 9


class TestDedupFields:
    def test_build_dedup_fields_liefert_url_und_fingerabdruck(self):
        canonical, dedup = build_dedup_fields(
            platform="test",
            url="https://example.invalid/a?utm_source=x",
            title="RTX 3080",
            total_price=210.0,
            seller_name="techsammler",
        )
        assert canonical == canonical_url("https://example.invalid/a")
        assert dedup

    def test_upsert_ohne_neue_daten_bleibt_unveraendert(self, db_session):
        payload = build_payload()
        listing, outcome = upsert_listing(db_session, payload)
        assert outcome == OUTCOME_NEW
        again, second_outcome = upsert_listing(db_session, payload)
        assert second_outcome == OUTCOME_UNCHANGED
        assert again.id == listing.id


class TestDefekteUndNeueWare:
    """Defekte Angebote und Neuware gehören nicht in eine Gebraucht-Suche."""

    def test_zustand_defekt_wird_verworfen(self, ingestor):
        offer = ingestor(max_price=400)
        outcome = offer.precheck(raw(condition_text="Als Ersatzteil / defekt"))
        assert outcome is not None
        assert outcome.result == RESULT_DEFECT
        assert "Ersatzteil" in (outcome.reason or "")

    def test_zustand_neu_wird_verworfen(self, ingestor):
        offer = ingestor(max_price=400)
        outcome = offer.precheck(raw(condition_text="Neu: Sonstige (siehe Artikelbeschreibung)"))
        assert outcome is not None
        assert outcome.result == RESULT_NEW_ITEM
        assert "Neuware" in (outcome.reason or "")

    def test_defekt_im_titel_wird_verworfen(self, ingestor):
        offer = ingestor(max_price=400)
        outcome = offer.precheck(
            raw(
                title="GIGABYTE GeForce GTX 750 Ti OC 2GB Grafikkarte GV-N750OC-2GI Defekt",
                description=None,
            )
        )
        assert outcome is not None
        assert outcome.result == RESULT_DEFECT

    def test_funktionierende_karte_bleibt(self, ingestor):
        offer = ingestor(max_price=400)
        assert offer.precheck(raw()) is None

    def test_weiche_hinweise_verwerfen_nichts(self, ingestor):
        """"Keine Garantie" oder "ungetestet" sind keine Defekte."""
        offer = ingestor(max_price=400)
        outcome = offer.precheck(
            raw(
                title="ASUS RTX 3080 TUF Gaming 10GB Grafikkarte",
                description="Gebraucht, keine Garantie, Zustand nicht getestet.",
            )
        )
        assert outcome is None

    def test_verneinter_defekt_verwirft_nichts(self):
        """"kein Defekt" ist ein gutes Zeichen, kein Ausschlussgrund."""
        assert (
            defect_or_new_reason(
                "MSI RTX 3080 Gaming X Trio Grafikkarte",
                "Karte läuft, kein Defekt, keine Ersatzteilspender.",
                condition_text="Gebraucht",
            )
            is None
        )

    def test_lokaler_import_bleibt_verschont(self, ingestor):
        """Der lokale Import enthält die eigene Auswahl des Benutzers."""
        offer = ingestor(max_price=400)
        outcome = offer.precheck(
            raw(platform="local_import", title="RTX 3080 Grafikkarte defekt zum Basteln")
        )
        assert outcome is None

    def test_new_item_und_defekt_haben_eigene_ergebnisarten(self):
        assert RESULT_DEFECT != RESULT_TITLE_FILTER
        assert RESULT_NEW_ITEM != RESULT_TITLE_FILTER


def build_payload():
    from backend.services.repository import ListingPayload

    return ListingPayload(
        platform="test",
        url="https://example.invalid/a",
        title="RTX 3080",
        total_price=210.0,
        listing_format="fixed_price",
        model="RTX 3080",
        category="gpu",
    )
