"""Tests für den eBay-Website-Connector (ohne echten Browser).

Geprüft wird die reine Logik: Preis-/Titelanalyse, URL-Bau, Karten-Auswertung
und das ehrliche Verhalten bei Bot-Blockade. Es wird nie eine echte
Netzwerkverbindung oder ein Browser gestartet.
"""

from __future__ import annotations

import pytest

from backend.connectors.base import LocationFilter, SearchQuerySpec
from backend.connectors.ebay_web import (
    EbayWebConnector,
    _clean_title,
    _effective_page_size,
    _german_float,
    _parse_price,
    _parse_total,
)


class _DisabledSettings:
    ebay_web_enabled = False
    ebay_web_timeout_seconds = 5.0
    ebay_web_max_concurrency = 1
    ebay_web_max_results_per_query = 480


class _EnabledSettings:
    ebay_web_enabled = True
    ebay_web_timeout_seconds = 5.0
    ebay_web_max_concurrency = 1
    ebay_web_max_results_per_query = 480


class TestPreisanalyse:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("EUR 149,99", 149.99),
            ("EUR 1.234,56", 1234.56),
            ("EUR 1.234", 1234.0),
            ("$20.00", 20.0),
            ("Gratis", 0.0),
            ("EUR 37", 37.0),
            (None, None),
            ("", None),
            ("keine Zahl", None),
        ],
    )
    def test_preise_werden_korrekt_gelesen(self, raw, expected):
        assert _parse_price(raw) == expected

    def test_zahlenformat_deutsch(self):
        assert _german_float("1.234,56") == 1234.56
        assert _german_float("1.234") == 1234.0
        assert _german_float("149,99") == 149.99

    def test_trefferzahl_aus_ueberschrift(self):
        assert _parse_total("47 Ergebnisse für rtx 3060") == 47
        assert _parse_total("1.234 Ergebnisse für grafikkarte") == 1234
        assert _parse_total("keine Zahl") is None
        assert _parse_total(None) is None


class TestTitelbereinigung:
    def test_fensterhinweis_und_badge_werden_entfernt(self):
        raw = "NEUES ANGEBOT MSI GeForce RTX 3060 Wird in neuem Fenster oder Tab geöffnet"
        assert _clean_title(raw) == "MSI GeForce RTX 3060"

    def test_leerer_titel(self):
        assert _clean_title(None) == ""
        assert _clean_title("   ") == ""


class TestSeitengroesse:
    def test_rundung_auf_erlaubte_werte(self):
        assert _effective_page_size(1) == 60
        assert _effective_page_size(50) == 60
        assert _effective_page_size(60) == 60
        assert _effective_page_size(100) == 120
        assert _effective_page_size(500) == 240


class TestUrlBau:
    def _connector(self):
        return EbayWebConnector(_EnabledSettings())

    def test_basis_url_enthaelt_suchbegriff(self):
        connector = self._connector()
        query = SearchQuerySpec(term="gtx 1660 super", platform="ebay_web")
        url = connector._build_url(query, page_size=60, page=1)
        assert url.startswith("https://www.ebay.de/sch/i.html?")
        assert "_nkw=gtx+1660+super" in url
        assert "_ipg=60" in url
        assert "_pgn" not in url

    def test_seite_und_sofortkauf(self):
        connector = self._connector()
        query = SearchQuerySpec(
            term="rtx 3060",
            platform="ebay_web",
            max_price=250,
            min_price=50,
            fixed_price_only=True,
        )
        url = connector._build_url(query, page_size=60, page=2)
        assert "_pgn=2" in url
        assert "LH_BIN=1" in url

    def test_preisrahmen_geht_an_ebay(self):
        """Mindestpreis und Kauf-Limit gehen als _udlo/_udhi an eBay.

        Damit lädt die Suche die überteuerten Treffer gar nicht erst. Live
        vermessen (13.10.2026): eBay füllt einen engen Rahmen mit ähnlichen
        Artikeln auf - die Titelprüfung verwirft sie. Die letzte Instanz bleibt
        deshalb die Ingestion, die jedes Angebot erneut am Limit messen muss.
        """
        connector = self._connector()
        query = SearchQuerySpec(
            term="gtx 1660",
            platform="ebay_web",
            max_price=50,
            min_price=12.5,
            fixed_price_only=True,
        )
        url = connector._build_url(query, page_size=60, page=1)
        assert "_udhi=50" in url
        assert "_udlo=12.5" in url

    def test_ohne_preisrahmen_keine_preisparameter(self):
        """Ohne Limit und Mindestpreis wird kein Preisrahmen erfunden."""
        connector = self._connector()
        query = SearchQuerySpec(term="rtx 3060", platform="ebay_web")
        url = connector._build_url(query, page_size=60, page=1)
        assert "_udhi" not in url
        assert "_udlo" not in url

    def test_zustandsfilter_wird_gemappt(self):
        connector = self._connector()
        query = SearchQuerySpec(
            term="rtx 3060",
            platform="ebay_web",
            condition_texts=["Gebraucht"],
        )
        url = connector._build_url(query, page_size=60, page=1)
        assert "LH_ItemCondition=3000" in url

    def test_gebrauchte_zustaende_als_rangzahlen(self):
        """Der Suchlauf übergibt die gebrauchten Zustände als Rangzahlen.

        Damit liefert eBay von vornherein keine Neuware - die Anwendung würde
        sie ohnehin verwerfen. "Gebraucht" (3000) kommt hinzu: Items ohne
        Unterzustand tragen nur diesen Code.
        """
        connector = self._connector()
        query = SearchQuerySpec(term="rtx 3060", platform="ebay_web", condition_texts=["1", "2", "3"])
        url = connector._build_url(query, page_size=60, page=1)
        assert "LH_ItemCondition=6000%7C5000%7C4000%7C3000" in url
        assert "1000" not in url  # keine Neuware
        assert "7000" not in url  # keine defekte Ware

    def test_nur_der_mindestrang_wird_uebernommen(self):
        connector = self._connector()
        query = SearchQuerySpec(term="rtx 3060", platform="ebay_web", condition_texts=["3"])
        url = connector._build_url(query, page_size=60, page=1)
        assert "LH_ItemCondition=4000%7C3000" in url

    def test_neueste_ansicht_sortiert_neu(self):
        """Die Ansicht "neueste zuerst" ist die einzige mit Sortierung."""
        connector = self._connector()
        query = SearchQuerySpec(
            term="rtx 3060", platform="ebay_web", days_back=7, sort="newest"
        )
        url = connector._build_url(query, page_size=60, page=1)
        assert "_sop=10" in url

    def test_standardsicht_bleibt_relevanzsortiert(self):
        """Der Zeitraum wird nicht mehr über die Sortierung angenähert: Die
        Standardsicht bleibt die Relevanzsortierung, die frische Angebote
        liefert die zweite Ansicht (siehe sort_variants)."""
        connector = self._connector()
        query = SearchQuerySpec(term="rtx 3060", platform="ebay_web", days_back=7)
        url = connector._build_url(query, page_size=60, page=1)
        assert "_sop" not in url
        assert connector.sort_variants == ("newest",)

    def test_kein_standortparameter_im_url(self):
        """_stpos/_sadis wurden live geprueft und veraendern nichts - also weg."""
        connector = self._connector()
        query = SearchQuerySpec(
            term="rtx 3060",
            platform="ebay_web",
            location=LocationFilter(postal_code="10115", radius_km=25.0),
        )
        url = connector._build_url(query, page_size=60, page=1)
        assert "_stpos" not in url
        assert "_sadis" not in url

    def test_standortfilter_wird_nicht_behauptet(self):
        """Die Website kann den Artikelstandort weder filtern noch anzeigen."""
        assert EbayWebConnector(_EnabledSettings()).capabilities.supports_location_filter is False


class TestSeitengroesseKonfiguriert:
    """Größere Seiten liefern dieselbe Liste in weniger Anfragen."""

    class _MitGroesse(_EnabledSettings):
        ebay_web_page_size = 240

    class _OhneGroesse(_EnabledSettings):
        pass

    def test_konfigurierte_groesse_wird_angefragt(self):
        connector = EbayWebConnector(self._MitGroesse())
        query = SearchQuerySpec(term="rtx 3060", platform="ebay_web", page_size=50)
        assert connector.requested_page_size(query) == 240

    def test_ohne_einstellung_gilt_der_standard(self):
        from backend.connectors.ebay_web import DEFAULT_CONFIGURED_PAGE_SIZE

        connector = EbayWebConnector(self._OhneGroesse())
        query = SearchQuerySpec(term="rtx 3060", platform="ebay_web", page_size=50)
        assert connector.requested_page_size(query) == DEFAULT_CONFIGURED_PAGE_SIZE

    def test_groessere_anfrage_bleibt_erhalten(self):
        connector = EbayWebConnector(self._OhneGroesse())
        query = SearchQuerySpec(term="rtx 3060", platform="ebay_web", page_size=240)
        assert connector.requested_page_size(query) == 240

    @pytest.mark.asyncio()
    async def test_kleinere_lieferung_verschiebt_die_schrittweite(self):
        """Liefert eBay kleiner als angefragt, darf keine Lücke entstehen."""
        connector = EbayWebConnector(self._MitGroesse())
        items = [
            {
                "url": f"https://www.ebay.de/itm/{1000 + index}",
                "listing_id": str(1000 + index),
                "title": f"RTX 3060 Nummer {index}",
                "price": "EUR 100,00",
                "text": "RTX 3060 EUR 100,00 Sofort-Kaufen",
            }
            for index in range(60)
        ]
        seen_urls: list[str] = []

        async def fake_fetch(url):
            seen_urls.append(url)
            return {"items": items, "heading": "1.200 Ergebnisse für rtx 3060"}

        connector._fetch = fake_fetch  # type: ignore[assignment]
        query = SearchQuerySpec(term="rtx 3060", platform="ebay_web")
        first = await connector.search_page(query)
        assert first.meta["page_size"] == 60  # gemerkt: eBay lieferte nur 60
        assert first.has_next is True
        assert first.next_offset == 60
        second = await connector.search_page(query.with_offset(first.next_offset))
        assert second.meta["page"] == 2
        assert "_ipg=60" in seen_urls[-1]
        assert first.next_offset == 60


class TestKartenauswertung:
    def _connector(self):
        return EbayWebConnector(_EnabledSettings())

    def test_karte_wird_zu_angebot(self):
        connector = self._connector()
        item = {
            "url": "https://www.ebay.de/itm/407202904985",
            "listing_id": "407202904985",
            "title": "Palit GTX1660 super 6GB GDDR6 Wird in neuem Fenster oder Tab geöffnet",
            "price": "EUR 120,00",
            "subtitle": "Gebraucht | Gewerblich",
            "text": (
                "Palit GTX1660 super EUR 120,00 Sofort-Kaufen Gratis Lieferung "
                "restoredandresold 100% positiv (681)"
            ),
            "image": "https://i.ebayimg.com/images/g/x/s-l500.webp",
        }
        listing = connector._to_raw_listing(item)
        assert listing is not None
        assert listing.platform == "ebay_web"
        assert listing.platform_listing_id == "407202904985"
        assert listing.title == "Palit GTX1660 super 6GB GDDR6"
        assert listing.price == 120.0
        assert listing.condition_text == "Gebraucht"
        assert listing.platform_format == "FIXED_PRICE"
        assert listing.seller_name == "restoredandresold"
        assert listing.seller_feedback_percent == 100.0

    def test_karte_ohne_titel_wird_verworfen(self):
        connector = self._connector()
        assert connector._to_raw_listing({"url": "https://www.ebay.de/itm/1"}) is None

    def test_auktion_wird_erkannt(self):
        connector = self._connector()
        item = {
            "url": "https://www.ebay.de/itm/123",
            "title": "RTX 3080 Auktion",
            "price": "EUR 300,00",
            "text": "RTX 3080 Auktion EUR 300,00 5 Gebote",
        }
        listing = connector._to_raw_listing(item)
        assert listing is not None
        assert listing.platform_format == "AUCTION"


class TestCapabilities:
    def test_deaktiviert_ist_nicht_verfuegbar(self):
        caps = EbayWebConnector(_DisabledSettings()).capabilities
        assert caps.available is False
        assert caps.reason_unavailable
        assert caps.requires_credentials is False

    def test_aktiv_ist_verfuegbar_ohne_zugangsdaten(self):
        caps = EbayWebConnector(_EnabledSettings()).capabilities
        assert caps.configured is True
        assert caps.available is True
        assert caps.supports_search is True
        assert caps.requires_credentials is False


class TestBlockade:
    @pytest.mark.asyncio
    async def test_blockade_wird_ehrlich_gemeldet(self):
        connector = EbayWebConnector(_EnabledSettings())

        async def fake_fetch(url):  # pragma: no cover - Verhalten wird geprüft
            return {"blocked": True, "items": []}

        connector._fetch = fake_fetch  # type: ignore[assignment]
        query = SearchQuerySpec(term="rtx 3060", platform="ebay_web")
        page = await connector.search_page(query)
        assert page.listings == []
        assert page.meta.get("blocked") is True
        assert any("blockiert" in text for text in page.limitations)
