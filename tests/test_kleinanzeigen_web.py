"""Tests für den Kleinanzeigen-Website-Connector (ohne echten Browser).

Geprüft wird die reine Logik: Preis-, Datums- und Trefferanalyse, URL-Bau,
Karten-Auswertung, Paginierung und das ehrliche Verhalten bei Bot-Blockade.
Es wird nie eine echte Netzwerkverbindung oder ein Browser gestartet.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from backend.connectors.base import SearchQuerySpec
from backend.connectors.kleinanzeigen_web import (
    PAGE_SIZE,
    KleinanzeigenWebConnector,
    _EXTRACT_JS,
    _absolute_url,
    _clean_text,
    _derive_condition,
    _id_from_url,
    _parse_location,
    _parse_price,
    _parse_published,
    _parse_total,
)
from backend.domain import normalize
from backend.search.ingest import defect_or_new_reason


class _DisabledSettings:
    kleinanzeigen_web_enabled = False


class _EnabledSettings:
    kleinanzeigen_web_enabled = True
    kleinanzeigen_web_timeout_seconds = 5.0
    kleinanzeigen_web_max_concurrency = 1
    kleinanzeigen_web_max_results_per_query = 250
    kleinanzeigen_web_profile_dir = ""


def _connector() -> KleinanzeigenWebConnector:
    return KleinanzeigenWebConnector(_EnabledSettings())


def _card(**overrides) -> dict:
    """Eine Angebotskarte, wie sie die Seitenextraktion liefert."""
    card = {
        "url": "/s-anzeige/asus-nvidia-geforce-rtx-5060ti-prime-16gb/3534050283-225-6975",
        "listing_id": "3534050283",
        "title": "Asus Nvidia Geforce RTX 5060Ti Prime 16gb",
        "description": "Top-Zustand - nur kurz als Übergangslösung genutzt ...",
        "price": "700 € VB",
        "location": "96346 Wallenfels",
        "published": "Heute, 20:11",
        "badges": ["Direkt kaufen", "Versand möglich"],
        "image": "https://img.kleinanzeigen.de/api/v1/prod-ads/images/5c/abc?rule=$_2.AUTO",
        "text": "96346 Wallenfels Heute, 20:11 Asus Nvidia Geforce RTX 5060Ti 700 € VB",
    }
    card.update(overrides)
    return card


class TestPreisanalyse:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("700 € VB", 700.0),
            ("1.329 €", 1329.0),
            ("15 €", 15.0),
            ("1.234,56 €", 1234.56),
            ("Zu verschenken", 0.0),
            (" VB", None),
            ("", None),
            (None, None),
        ],
    )
    def test_preise_werden_korrekt_gelesen(self, raw, expected):
        assert _parse_price(raw) == expected


class TestTrefferzahl:
    def test_trefferzahl_aus_ueberschrift(self):
        heading = '1 - 25 von 1.228 Ergebnissen für „rtx 3060" in Deutschland'
        assert _parse_total(heading) == 1228

    def test_ohne_ueberschrift_keine_zahl(self):
        assert _parse_total(None) is None
        assert _parse_total("") is None
        assert _parse_total("Leider nichts gefunden") is None

    def test_einzelne_trefferzahl_als_rueckfall(self):
        assert _parse_total("18 Ergebnisse für grafikkarte") == 18


class TestStandort:
    def test_plz_und_ort_werden_getrennt(self):
        postal_code, city = _parse_location("96346 Wallenfels")
        assert postal_code == "96346"
        assert city == "Wallenfels"

    def test_ohne_plz_bleibt_die_plz_leer(self):
        postal_code, city = _parse_location("Wallenfels")
        assert postal_code is None
        assert city == "Wallenfels"

    def test_leere_angabe(self):
        assert _parse_location(None) == (None, None)


class TestDatumsanalyse:
    NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)

    def test_heute_mit_uhrzeit(self):
        result = _parse_published("Heute, 20:11", now=self.NOW)
        assert result is not None
        local = result.astimezone()
        assert (local.hour, local.minute) == (20, 11)
        assert local.date() == self.NOW.astimezone().date()

    def test_gestern_mit_uhrzeit(self):
        result = _parse_published("Gestern, 08:06", now=self.NOW)
        assert result is not None
        local = result.astimezone()
        assert (local.hour, local.minute) == (8, 6)
        assert local.date() < self.NOW.astimezone().date()

    def test_absolutes_datum(self):
        result = _parse_published("08.10.2026", now=self.NOW)
        assert result is not None
        assert result.astimezone().date() == date(2026, 10, 8)

    def test_kurzes_datum_nutzt_das_laufende_jahr(self):
        result = _parse_published("08.10.", now=self.NOW)
        assert result is not None
        assert result.astimezone().date() == date(2026, 10, 8)

    @pytest.mark.parametrize("raw", [None, "", "vor kurzem", "32.13.2026"])
    def test_unbekanntes_format_erfindet_kein_datum(self, raw):
        assert _parse_published(raw, now=self.NOW) is None


class TestTitelUndUrl:
    def test_leerzeichen_werden_bereinigt(self):
        assert _clean_text("  MSI   RTX\t3060 \n") == "MSI RTX 3060"
        assert _clean_text(None) == ""

    def test_relative_links_werden_absolut(self):
        url = _absolute_url("/s-anzeige/test/123-225-3660")
        assert url == "https://www.kleinanzeigen.de/s-anzeige/test/123-225-3660"

    def test_query_parameter_werden_entfernt(self):
        url = _absolute_url("/s-anzeige/test/123-225-3660?foo=bar")
        assert url == "https://www.kleinanzeigen.de/s-anzeige/test/123-225-3660"

    def test_fremde_ziele_werden_verworfen(self):
        assert _absolute_url("https://example.com/s-anzeige/x/123-1-1") == ""
        assert _absolute_url("kein-link") == ""
        assert _absolute_url(None) == ""
        # Auch Links auf der Plattform, aber außerhalb einer Anzeige, zählen nicht.
        assert _absolute_url("/s-rtx-3060/k0") == ""

    def test_kennung_aus_dem_link(self):
        url = "/s-anzeige/asus-rtx-5060ti/3534050283-225-6975"
        assert _id_from_url(url) == "3534050283"
        assert _id_from_url("") is None


class TestUrlBau:
    def test_basis_url_enthaelt_suchbegriff(self):
        query = SearchQuerySpec(term="rtx 3060", platform="kleinanzeigen_web")
        url = _connector()._build_url(query, page=1)
        assert url.startswith("https://www.kleinanzeigen.de/s-rtx%203060/k0?")
        assert "pageNum" not in url
        assert "sortingField=SORTING_DATE" in url

    def test_seite_preisgrenzen_und_sortierung(self):
        query = SearchQuerySpec(
            term="rtx 3060",
            platform="kleinanzeigen_web",
            max_price=200,
            min_price=50,
        )
        url = _connector()._build_url(query, page=3)
        assert "pageNum=3" in url
        assert "maxPrice=200" in url
        assert "minPrice=50" in url

    def test_leerer_suchbegriff_nutzt_den_probetext(self):
        query = SearchQuerySpec(term="  ", platform="kleinanzeigen_web")
        url = _connector()._build_url(query, page=1)
        assert url.startswith("https://www.kleinanzeigen.de/s-grafikkarte/k0")

    def test_versandsuche_nutzt_den_plattformfilter(self):
        """'Nur Versand' soll die Plattform selbst filtern, nicht erst die Liste."""
        query = SearchQuerySpec(
            term="rtx 3060", platform="kleinanzeigen_web", shipping_only=True, max_price=200
        )
        url = _connector()._build_url(query, page=2)
        assert url.startswith("https://www.kleinanzeigen.de/s-versand:ja/rtx%203060/k0?")
        assert "pageNum=2" in url
        assert "maxPrice=200" in url

    def test_ohne_versandsuche_bleibt_der_pfad_unveraendert(self):
        query = SearchQuerySpec(term="rtx 3060", platform="kleinanzeigen_web")
        url = _connector()._build_url(query, page=1)
        assert "/s-versand:ja/" not in url
        assert url.startswith("https://www.kleinanzeigen.de/s-rtx%203060/k0?")

    def test_suchschluessel_unterscheidet_die_versandsuche(self):
        basis = SearchQuerySpec(term="rtx 3060", platform="kleinanzeigen_web")
        versand = SearchQuerySpec(
            term="rtx 3060", platform="kleinanzeigen_web", shipping_only=True
        )
        assert basis.key() != versand.key()


class TestKartenauswertung:
    def test_karte_wird_zu_angebot(self):
        listing = _connector()._to_raw_listing(_card())
        assert listing is not None
        assert listing.platform == "kleinanzeigen_web"
        assert listing.platform_listing_id == "3534050283"
        assert listing.title == "Asus Nvidia Geforce RTX 5060Ti Prime 16gb"
        assert listing.url == (
            "https://www.kleinanzeigen.de/s-anzeige/"
            "asus-nvidia-geforce-rtx-5060ti-prime-16gb/3534050283-225-6975"
        )
        assert listing.price == 700.0
        # Kleinanzeigen kennt keine Auktionen: alles ist Festpreis.
        assert listing.platform_format == "FIXED_PRICE"
        assert listing.buying_options == ["FIXED_PRICE"]
        assert listing.published_at is not None
        assert listing.postal_code == "96346"
        assert listing.location_text == "Wallenfels"
        assert listing.image_url and listing.image_url.startswith("https://")
        assert listing.extra["price_text"] == "700 € VB"

    def test_zu_verschenken_ist_null_euro(self):
        listing = _connector()._to_raw_listing(_card(price="Zu verschenken"))
        assert listing is not None
        assert listing.price == 0.0

    def test_verhandlungsbasis_ohne_zahl_bleibt_unbekannt(self):
        listing = _connector()._to_raw_listing(_card(price="VB"))
        assert listing is not None
        assert listing.price is None

    def test_zustand_bleibt_unbekannt_ohne_hinweis_im_text(self):
        """Nennt der Anzeigetext keinen Zustand, bleibt er leer statt geschätzt."""
        listing = _connector()._to_raw_listing(_card())
        assert listing is not None
        assert listing.condition_text is None
        assert listing.extra["condition_from_text"] is False

    def test_versandhinweis_nur_aus_der_liste(self):
        listing = _connector()._to_raw_listing(_card(badges=["Versand möglich"]))
        assert listing is not None
        assert listing.pickup_available is False
        assert listing.extra["shipping_possible"] is True

    def test_nur_abholung_wird_erkannt(self):
        listing = _connector()._to_raw_listing(_card(badges=["Nur Abholung"]))
        assert listing is not None
        assert listing.pickup_available is True

    def test_karte_ohne_titel_oder_link_wird_verworfen(self):
        assert _connector()._to_raw_listing(_card(title="")) is None
        assert _connector()._to_raw_listing(_card(url="")) is None

    def test_fremder_link_wird_verworfen(self):
        assert _connector()._to_raw_listing(_card(url="https://example.com/x")) is None


class TestZustandAusDemAnzeigetext:
    """Kleinanzeigen nennt in der Liste keinen Zustand - er steht im Text.

    Ohne diese Ableitung wird jedes Angebot der Quelle mit "Zustand unbekannt"
    bewertet (Abzug in der Bewertung), obwohl der Verkäufer ihn genannt hat.
    """

    def _listing(self, **overrides):
        listing = _connector()._to_raw_listing(_card(**overrides))
        assert listing is not None
        return listing

    def test_ohne_text_kein_zustand(self):
        assert _derive_condition("", None, "   ") is None

    def test_gebraucht_wird_erkannt(self):
        listing = self._listing(description="Voll funktionsfähig, gebraucht.")
        assert listing.condition_text == "Gebraucht, gut"
        assert listing.extra["condition_from_text"] is True

    def test_wie_neu_schlaegt_das_generische_gebraucht(self):
        listing = self._listing(
            title="ASUS RTX 3070 Grafikkarte",
            description="Gebraucht, aber wie neu - kaum genutzt.",
        )
        assert listing.condition_text == "Sehr gut / wie neu"

    def test_neu_aus_dem_freitext_bleibt_unangetastet(self):
        """"Neu" im Freitext darf kein Angebot löschen ("neues System")."""
        listing = self._listing(
            title="MSI GeForce GTX 1660 super Grafikkarte",
            description="Sie ist bereit für den Einsatz in einem neuen System.",
        )
        assert listing.condition_text is None

    def test_defekt_aus_dem_freitext_bleibt_unangetastet(self):
        """Verneinte Defekte ("Keine Fehler oder Defekte") dürfen nichts löschen."""
        listing = self._listing(
            title="AMD Ryzen 9 7950X3D",
            description="Er wurde nur zum Spielen genutzt. Keine Fehler oder Defekte!",
        )
        assert listing.condition_text is None

    def test_abgeleiteter_zustand_wird_nie_ausgeschlossen(self):
        """Was hier entsteht, muss die Ingestion durchlaufen - sonst wäre es ein
        stiller Angebotsverlust aus dem Freitext."""
        listing = self._listing(description="Die Karte ist wie neu, kaum gebraucht.")
        rank = normalize.resolve_condition_rank(listing.condition_text)
        assert rank in normalize.SEARCHABLE_CONDITIONS
        assert (
            defect_or_new_reason(
                listing.title,
                listing.description,
                condition_text=listing.condition_text,
            )
            is None
        )


class TestSeitenschema:
    def test_auslese_javascript_kennt_beide_seitenaufbauten(self):
        """Der aktuelle Aufbau ist <article data-href>, der alte li.aditem."""
        assert "article[data-href]" in _EXTRACT_JS
        assert "li.aditem" in _EXTRACT_JS
        assert "s-anzeige" in _EXTRACT_JS

    def test_bereiter_warteselektor_passt_zum_seitenaufbau(self):
        connector = _connector()
        assert "article[data-href]" in connector.ready_selector
        assert connector.warmup_url.startswith("https://www.kleinanzeigen.de")


class TestCapabilities:
    def test_deaktiviert_ist_nicht_verfuegbar(self):
        caps = KleinanzeigenWebConnector(_DisabledSettings()).capabilities
        assert caps.available is False
        assert caps.reason_unavailable
        assert caps.requires_credentials is False

    def test_aktiv_ist_verfuegbar_ohne_zugangsdaten(self):
        caps = KleinanzeigenWebConnector(_EnabledSettings()).capabilities
        assert caps.configured is True
        assert caps.available is True
        assert caps.supports_search is True
        assert caps.requires_credentials is False
        assert caps.page_size_max == PAGE_SIZE
        assert caps.provides_published_date is True
        # Ehrliche Grenzen: kein Zustandsfilter, kein Umkreisfilter.
        assert caps.supports_condition_filter is False
        assert caps.supports_location_filter is False


class TestPaginierungUndBlockade:
    @pytest.mark.asyncio()
    async def test_blockade_wird_ehrlich_gemeldet(self):
        connector = _connector()

        async def fake_fetch(url):  # pragma: no cover - Verhalten wird geprüft
            return {"blocked": True, "items": []}

        connector._fetch = fake_fetch  # type: ignore[assignment]
        query = SearchQuerySpec(term="rtx 3060", platform="kleinanzeigen_web")
        page = await connector.search_page(query)
        assert page.listings == []
        assert page.meta.get("blocked") is True
        assert any("blockiert" in text for text in page.limitations)

    @pytest.mark.asyncio()
    async def test_weitere_seite_wenn_es_noch_treffer_gibt(self):
        connector = _connector()

        async def fake_fetch(url):  # pragma: no cover - Verhalten wird geprüft
            return {
                "items": [_card()],
                "heading": '1 - 25 von 60 Ergebnissen für „rtx 3060" in Deutschland',
            }

        connector._fetch = fake_fetch  # type: ignore[assignment]
        query = SearchQuerySpec(term="rtx 3060", platform="kleinanzeigen_web")
        page = await connector.search_page(query)
        assert page.count == 1
        assert page.total_reported == 60
        assert page.has_next is True
        assert page.next_offset == PAGE_SIZE

    @pytest.mark.asyncio()
    async def test_zweite_seite_wird_ueber_pagenum_geladen(self):
        connector = _connector()
        seen: list[str] = []

        async def fake_fetch(url):  # pragma: no cover - Verhalten wird geprüft
            seen.append(url)
            return {
                "items": [_card()],
                "heading": '26 - 50 von 60 Ergebnissen für „rtx 3060" in Deutschland',
            }

        connector._fetch = fake_fetch  # type: ignore[assignment]
        query = SearchQuerySpec(term="rtx 3060", platform="kleinanzeigen_web")
        await connector.search_page(query.with_offset(PAGE_SIZE))
        assert seen and "pageNum=2" in seen[0]

    @pytest.mark.asyncio()
    async def test_letzte_seite_hat_kein_naechstes(self):
        connector = _connector()

        async def fake_fetch(url):  # pragma: no cover - Verhalten wird geprüft
            return {
                "items": [_card()],
                "heading": '1 - 18 von 18 Ergebnissen für „rtx 3060" in Deutschland',
            }

        connector._fetch = fake_fetch  # type: ignore[assignment]
        query = SearchQuerySpec(term="rtx 3060", platform="kleinanzeigen_web")
        page = await connector.search_page(query)
        assert page.has_next is False
        assert page.next_offset is None

    @pytest.mark.asyncio()
    async def test_leere_seite_meldet_einschraenkung(self):
        connector = _connector()

        async def fake_fetch(url):  # pragma: no cover - Verhalten wird geprüft
            return {"items": [], "heading": None}

        connector._fetch = fake_fetch  # type: ignore[assignment]
        query = SearchQuerySpec(term="gibtesnicht", platform="kleinanzeigen_web")
        page = await connector.search_page(query)
        assert page.listings == []
        assert any("keine lesbaren Angebote" in text for text in page.limitations)
